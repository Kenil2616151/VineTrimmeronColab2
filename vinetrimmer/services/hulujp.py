import hashlib
import json
import os
import re
import sys
import time
import uuid

import click
import requests

from vinetrimmer.objects import Title, TextTrack, Tracks
from vinetrimmer.services.BaseService import BaseService


class HuluJP(BaseService):
    """
    Service code for the Hulu Japan streaming service (https://hulu.jp).

    \b
    Authorization: Credentials
    Security: HD@L3
    """

    ALIASES = ["HULUJP", "hulujapan"]
    #GEOFENCE = ["jp"]
    TITLE_RE = r"^(?:https?://(?:www\.)?hulu\.jp/)(?P<id>[a-z0-9-]+)"

    CODEC_MAP = {
        "H264": "avc",
        "H265": "hevc",
        "VP9": "vp9",
    }

    @staticmethod
    @click.command(name="HuluJP", short_help="https://hulu.jp")
    @click.argument("title", type=str, required=False)
    @click.pass_context
    def cli(ctx, **kwargs):
        return HuluJP(ctx, **kwargs)

    def __init__(self, ctx, title):
        self.parse_title(ctx, title)
        super().__init__(ctx)

        self.profile = ctx.obj.profile

        self.license_url = None
        self.react_context = {}
        self.tokens = {}
        self.vuid = None

        self.vcodec = ctx.parent.params["vcodec"]

        self.configure()

    def get_titles(self):
        src = requests.get(
            url=f"https://www.hulu.jp/{self.title}",
            headers={
                "User-Agent": self.config["user_agent_browser"]
            }
        ).text
        match = re.search(r"window\.app=({.+);</script>", src, re.MULTILINE)

        if not match:
            raise self.log.exit(" - Failed to falcorCache data, check the slug.")
        falcor_cache_raw = match.group(1)
        falcor_cache = json.loads(re.sub(r"\\x", r"\\u00", falcor_cache_raw))['falcorCache']

        meta_id = next(iter(falcor_cache['titleSlug'].values()))["value"][1]

        series_metas = self.session.get(
            url=self.config["endpoints"]["metas"].format(id=meta_id),
            params=self.config["meta_params"],
        ).json()
        if not series_metas:
            raise self.log.exit(" - Unable to get metadata. Is the title ID correct?")

        titles = []

        for season in series_metas["seasons"]:
            metas = self.session.get(
                url=self.config["endpoints"]["metas_children"].format(id=season["id"]),
                params={
                    **self.config["common_params"],
                    **self.config["meta_params"],
                    **self.config["search_params"],
                }
            ).json()["metas"]

            titles += [Title(
                id_=meta["meta_id"],
                type_=Title.Types.TV,
                name=meta["series_name"],
                season=int(meta["season_number"]),
                episode=int(meta["episode_number"]),
                episode_name=meta["short_name"],
                original_lang=meta["original_audio_language"]["value"],
                source="HULU",
                service_data=meta
            ) for meta in metas]

        return titles

    def get_tracks(self, title, retrying=False):
        try:
            r = self.session.post(
                url=self.config["endpoints"]["playback_auth"],
                json={
                    **{
                        "meta_id": f"asset:{title.service_data['id_in_schema']}",
                        "vuid": self.vuid,
                        "with_resume_point": False,
                        "user_id": int(self.tokens["id"]),
                    },
                    **self.config["common_params"],
                },
            )
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 403 and not retrying:
                # 60 seconds appears to be the magic number to get requests to work again
                self.log.warning(" - Possible rate limit, retrying after 60 seconds.")

                for remaining in range(60, 0, -1):
                    sys.stdout.write("\r")
                    sys.stdout.write("{:02d} seconds remaining.".format(remaining))
                    sys.stdout.flush()
                    time.sleep(1)

                return self.get_tracks(title, retrying=True)
            else:
                raise

        auth = r.json()

        medias = self.session.get(
            url=self.config["endpoints"]["playback_medias"].format(id=auth["media"]["ovp_video_id"]),
            params={
                **self.config["common_params"],
                **{
                    "codecs": self.CODEC_MAP[self.vcodec],
                    "user_id": self.tokens["id"],
                }
            },
            headers={
                "X-Playback-Session-Id": auth["playback_session_id"],
            }
        ).json()

        best_source = max([x for x in medias["sources"] if x["label"] == "dash_cenc"],
                          key=lambda x: int(x['resolution'].split('x')[1]))

        self.license_url = best_source["key_systems"]["com.widevine.alpha"]["license_url"]

        tracks = Tracks.from_mpd(
            url=best_source["src"],
            session=self.session,
            source="HULU",
        )

        for sub_lang in ["en", "ja"]:
            for sub_type in ["normal", "forced", "cc"]:
                sub_url = auth["media"]["values"].get(f"caption_{sub_lang}_{sub_type}_standard")
                if sub_url:
                    tracks.add(TextTrack(
                        id_=hashlib.md5(sub_url.encode()).hexdigest()[0:6],
                        source="HULU",
                        url=sub_url,
                        # metadata
                        codec="vtt",
                        language=sub_lang,
                        forced=sub_type == "forced",
                        sdh=sub_type == "cc",
                    ))

        return tracks

    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        return self.config["certificate"]

    def license(self, challenge, track, **_):
        return self.session.post(
            url=self.license_url,
            data=challenge,  # expects bytes
            headers={
                "User-Agent": self.config["user_agent_browser"],
                "Content-Type": "application/octet-stream",
            },
        ).content

    # Service specific functions

    def configure(self):
        self.session.headers.update({
            "User-Agent": self.config["user_agent"],
        })

        self.tokens = self.get_tokens()
        self.vuid = uuid.uuid4().hex

        self.session.headers.update({
            "Authorization": f"Bearer {self.tokens['access_token']}",
            "X-Gaia-Authorization": f"extra {self.tokens['gaia_token']}",
            "X-Session-Token": self.tokens['session_token'],
            "X-User-Id": str(self.tokens['id']),
            "Content-Type": "application/json; charset=utf-8",
        })

    def get_tokens(self):
        session_data = self.session.get(
            url=self.config["endpoints"]["session_create"],
            params=self.config["auth_params"],
        ).json()

        # Try to get cached auth tokens
        tokens_cache_path = self.get_cache("tokens_{profile}.json".format(
            profile=self.profile,
        ))

        auth_needed = True
        tokens = {}
        if os.path.isfile(tokens_cache_path):
            with open(tokens_cache_path, encoding="utf-8") as fd:
                tokens = json.load(fd)

                if tokens:
                    try:
                        check = self.session.post(
                            url=self.config["endpoints"]["token_check"],
                            headers={
                                "X-Token-Id": str(tokens["id"]),
                                "Authorization": f"Bearer {tokens['access_token']}",
                                "Content-Type": "application/json"
                            }
                        ).json()
                        auth_needed = False if check["result"] else True
                    except requests.exceptions.HTTPError:
                        auth_needed = True

        if auth_needed:
            tokens = self.session.post(
                url=self.config["endpoints"]["user_auth"],
                json={
                    **{
                        "mail_address": self.credentials.username,
                        "password": self.credentials.password,
                    },
                    **self.config["common_params"],
                },
                headers={
                    "X-Gaia-Authorization": f"extra {session_data['gaia_token']}",
                    "X-Session-Token": session_data['session_token'],
                },
            ).json()

            os.makedirs(os.path.dirname(tokens_cache_path), exist_ok=True)
            with open(tokens_cache_path, "w", encoding="utf-8") as fd:
                json.dump(tokens, fd)

        return tokens
