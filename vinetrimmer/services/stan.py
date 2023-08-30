import base64
import json
import sys
import time
import urllib.parse
from hashlib import md5
from uuid import UUID

import click
import requests
from bs4 import BeautifulSoup
from Cryptodome.Hash import HMAC, SHA256

from vinetrimmer.objects import AudioTrack, TextTrack, Title, Tracks, VideoTrack
from vinetrimmer.services.BaseService import BaseService
from vinetrimmer.utils import Cdm, try_get
from vinetrimmer.utils.collections import as_list
from vinetrimmer.vendor.pymp4.parser import Box


class Stan(BaseService):
    """
    Service code for Nine Digital's Stan. streaming service (https://stan.com.au).

    \b
    Authorization: Credentials (TV), Cookies (Web)
    Security: UHD@L3, doesn't care about releases.
    """

    ALIASES = ["STAN"]
    #GEOFENCE = ["au"]
    TITLE_RE = [
        r"^(?:https?://play\.stan\.com\.au/programs/)?(?P<id>\d+)",
        r"^(?:https?://(?:www\.)?stan\.com\.au/watch/)?(?P<id>[a-z0-9-]+)",
    ]

    AUDIO_CODEC_MAP = {
        "AAC": "mp4a",
        "AC3": "ac-3",
        "EC3": "ec-3"
    }

    @staticmethod
    @click.command(name="Stan", short_help="https://stan.com.au")
    @click.argument("title", type=str, required=False)
    @click.option("-t", "--device-type", default="tv", type=click.Choice(["tv", "web"]),
                  help="Device type.")
    @click.option("-q", "--vquality", default="uhd", type=click.Choice(["uhd", "hd"]),
                  help="Quality to request from the manifest, combine --vquality uhd with --device-type tv for UHD L3.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return Stan(ctx, **kwargs)

    def __init__(self, ctx, title, device_type, vquality):
        super().__init__(ctx)
        self.parse_title(ctx, title)
        self.device_type = device_type
        self.vquality = vquality

        self.vcodec = ctx.parent.params["vcodec"].lower()
        self.acodec = ctx.parent.params["acodec"]
        self.range = ctx.parent.params["range_"]

        self.api_config = {}
        self.jwtoken = None
        self.license_api = None
        self.license_cd = None

        self.configure()

    def get_titles(self):
        if not self.title.isnumeric():
            r = self.session.get(self.config["endpoints"]["watch"].format(title_id=self.title))
            soup = BeautifulSoup(r.text, "lxml-html")
            data = json.loads(soup.select_one("script[type='application/ld+json']").text)
            self.title = data["@id"]

        r = self.session.get(f"{self.api_config['cat']['v12']}/programs/{self.title}.json")
        try:
            res = r.json()
        except json.JSONDecodeError:
            raise self.log.exit(f" - Failed to load title manifest: {r.text}")
        if "audioTracks" in res:
            res["original_language"] = [x["language"]["iso"] for x in res["audioTracks"] if x["type"] == "main"]
            if len(res["original_language"]) > 0:
                res["original_language"] = res["original_language"][0]

        original_language = res["original_language"]
        if not original_language:
            original_language = [x for x in res["audioTracks"] if x["type"] == "main"]
            if original_language:
                original_language = original_language[0]["language"]["iso"]
            else:
                original_language = res["languages"][0]

        if not res.get("seasons"):
            return Title(
                id_=self.title,
                type_=Title.Types.MOVIE,
                name=res["title"],
                year=res.get("releaseYear"),
                original_lang=original_language,
                source=self.ALIASES[0],
                service_data=res,
            )
        else:
            titles = []
            for season in res["seasons"]:
                r = self.session.get(season["url"])
                try:
                    season_res = r.json()
                except json.JSONDecodeError:
                    raise self.log.exit(f" - Failed to load season manifest: {r.text}")
                for episode in season_res["entries"]:
                    episode["title_year"] = res["releaseYear"]
                    episode["original_language"] = res.get("original_language")
                    titles.append(episode)
            return [Title(
                id_=x["id"],
                type_=Title.Types.TV,
                name=res["title"],
                year=x.get("title_year", x.get("releaseYear")),
                season=x.get("tvSeasonNumber"),
                episode=x.get("tvSeasonEpisodeNumber"),
                episode_name=x.get("title"),
                original_lang=original_language,
                source=self.ALIASES[0],
                service_data=x
            ) for x in titles]

    def get_tracks(self, title):
        program_data = self.session.get(
            f"{self.api_config['cat']['v12']}/programs/{title.service_data['id']}.json"
        ).json()

        try:
            r = self.session.get(
                url=program_data["streams"][self.vquality]["dash"]["auto"]["url"],
                params={
                    "jwToken": self.jwtoken,
                    "format": "json",
                    "capabilities.drm": "widevine",
                    "videoCodec": self.vcodec
                }
            )
        except requests.HTTPError as e:
            self.handle_error(e.response.json())
        except json.JSONDecodeError:
            raise self.log.exit(f" - Failed to load stream data: {r.text}")
        else:
            stream_data = r.json()
        stream_data = stream_data["media"]

        if self.vquality == "uhd":
            self.license_api = stream_data["fallbackDrm"]["licenseServerUrl"]
        else:
            self.license_api = stream_data["drm"]["licenseServerUrl"]
            self.license_cd = stream_data["drm"]["customData"]

        tracks = Tracks.from_mpd(
            data=self.session.get(
                url=self.config["endpoints"]["manifest"],
                params={
                    "url": stream_data["videoUrl"],
                    "audioType": "all"
                }
            ).text,
            url=self.config["endpoints"]["manifest"],
            source=self.ALIASES[0]
        )
        if self.acodec:
            tracks.audios = [
                x for x in tracks.audios
                if x.codec[:4] == self.AUDIO_CODEC_MAP[self.acodec]
            ]
        else:
            tracks.audios = [x for x in tracks.audios if not (x.codec[:4] == "mp4a" and x.bitrate == 448_000)]

        if "captions" in stream_data:
            for sub in stream_data["captions"]:
                tracks.add(TextTrack(
                    id_=md5(sub["url"].encode()).hexdigest()[0:6],
                    source=self.ALIASES[0],
                    url=sub["url"],
                    # metadata
                    codec=sub["type"].split("/")[-1],
                    language=sub["language"],
                    cc="(cc)" in sub["name"].lower()
                ))

        # craft pssh with the key_id
        # TODO: is doing this still necessary? since the code now tries grabbing PSSH from
        #       the first chunk of data of the track, it might be available from that.
        pssh = Box.parse(Box.build(dict(
            type=b"pssh",
            version=0,
            flags=0,
            system_ID=Cdm.uuid,
            init_data=b"\x12\x10" + UUID(stream_data["drm"]["keyId"]).bytes
        )))

        for track in tracks:
            track.needs_proxy = True
            if isinstance(track, VideoTrack):
                track.hdr10 = "/hdr/" in as_list(track.url)[0]
            if isinstance(track, (VideoTrack, AudioTrack)):
                track.encrypted = True
                if not track.pssh:
                    track.pssh = pssh

        return tracks

    def get_chapters(self, title):
        return []

    def certificate(self, **kwargs):
        # TODO: Hardcode the certificate
        return self.license(**kwargs)

    def license(self, challenge, **_):
        lic = self.session.post(
            url=self.license_api,
            headers={} if self.device_type == "tv" else {
                "dt-custom-data": self.license_cd
            },
            data=challenge  # expects bytes
        )
        try:
            if "license" in lic.json():
                return lic.json()["license"]  # base64 str?
        except json.JSONDecodeError:
            return lic.content  # bytes

        raise self.log.exit(f" - Failed to obtain license: {lic.text}")

    # Service specific functions

    def configure(self):
        self.log.info("Retrieving API configuration")
        self.api_config = self.get_config()
        self.log.info("Logging in")
        self.jwtoken = self.login()

    def get_config(self):
        res = self.session.get(
            self.config["endpoints"]["config"].format(type='web/app' if self.device_type == 'web' else 'tv/android'))
        try:
            return res.json()
        except json.JSONDecodeError:
            raise self.log.exit(f" - Failed to obtain Stan API configuration: {res.text}")

    def login(self):
        if self.session.cookies and self.device_type == "web":
            self.log.info(" + Using cookies")
            token = self.session.cookies.get("streamco_token")
            if not token:
                raise self.log.exit(" - No streamco_token cookie found, unable log in.")
            return token

        if not self.credentials:
            raise self.log.exit(" - No credentials provided, unable to log in.")
        self.session.get(self.config["endpoints"]["homepage"])  # need cookies
        try:
            res = self.session.post(
                url=self.api_config["login"]["v1"] + self.config["endpoints"]["login"].format(
                    type="web/account" if self.device_type == "web" else "mobile/account"
                ),
                data=(
                    {
                        "source": self.config["meta"]["login_source"],
                        "email": self.credentials.username,
                        "password": self.credentials.password
                    } if self.device_type == "web" else self.sign_payload({
                        "clientId": "242a1867d4f8ab23",
                        "os": "Android",
                        "tz": "Australia/Sydney",
                        "rnd": int(time.time()),
                        "stanName": "Stan-Android",
                        "stanVersion": "4.10.1",
                        "type": "console",
                        "manufacturer": "NVIDIA",
                        "password": self.credentials.password,
                        "model": "SHIELD Android TV",
                        "sdk": 28,
                        "email": self.credentials.username,
                        "screenSize": "3840x2160",
                        "hdcpVersion": "2.2",
                        "colorSpace": {"SDR": "sdr", "HDR10": "hdr10", "DV": "hdr"}.get(self.range),
                    })
                ),
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64; rv:81.0) Gecko/20100101 Firefox/81.0"
                        if self.device_type == "web" else
                        "Stan/Android/4.10.1; Dalvik/2.1.0 (Linux; U; Android 9; SHIELD Android TV Build/PPR1.180610.011)"  # noqa: E501
                    )
                }
            ).json()
        except requests.HTTPError as e:
            self.handle_error(e.response.json())
        return res["jwToken"]

    def sign_payload(self, payload):
        payload["sign"] = base64.b64encode(HMAC.new(
            self.config["hmac_key"].encode(),
            urllib.parse.urlencode(json.loads(json.dumps(payload, sort_keys=True))).encode("utf-8"),
            SHA256,
        ).digest()).decode()

        return payload

    def handle_error(self, res):
        if "errors" in res:
            for error in res["errors"]:
                code = error["code"]
                desc = (
                    try_get(self.api_config, lambda x: x["errors"][code]["messageTemplate"]) or "Unknown error"
                ).replace("\n\n", "\n")
                self.log.critical(f" - {desc} [{code}]")
            sys.exit(1)
