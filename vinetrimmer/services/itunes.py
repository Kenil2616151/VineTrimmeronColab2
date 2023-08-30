import base64
import itertools
import json
import os
import re
from enum import Enum
from urllib.parse import unquote

import click
import m3u8

from vinetrimmer.objects import AudioTrack, TextTrack, Title, Tracks, VideoTrack
from vinetrimmer.services.BaseService import BaseService
from vinetrimmer.vendor.pymp4.parser import Box


class iTunes(BaseService):
    """
    Service code for Apple's VOD streaming service (https://itunes.apple.com).

    \b
    Authorization: Cookies, Username/Password for Rentals
    Security: UHD@L1 FHD@L1 HD@L3
    """

    ALIASES = ["iT", "itunes"]
    TITLE_RE = r"^(?P<id>https://itunes\.apple\.com/.+/id\d+.*)"

    VIDEO_CODEC_MAP = {
        "H264": ["avc"],
        "H265": ["hvc", "hev", "dvh"]
    }
    AUDIO_CODEC_MAP = {
        "AAC": ["HE", "stereo"],
        "AC3": ["ac3"],
        "EC3": ["ec3", "atmos"]
    }

    @staticmethod
    @click.command(name="iTunes", short_help="https://itunes.apple.com")
    @click.argument("title", type=str, required=False)
    @click.pass_context
    def cli(ctx, **kwargs):
        return iTunes(ctx, **kwargs)

    def __init__(self, ctx, title):
        super().__init__(ctx)
        self.parse_title(ctx, title)

        self.vcodec = ctx.parent.params["vcodec"]
        self.acodec = ctx.parent.params["acodec"]

        self.profile = ctx.obj.profile

        self.extra_server_parameters = None
        self.rental_id = None
        self.rentals_supported = False

        self.configure()

    def get_titles(self):
        res = self.session.get(
            url=self.title,
            headers={
                'User-Agent': self.config["user_agent_browser"]
            }
        )
        match = re.search('id="shoebox-ember-data-store">(.+?)</script>', res.text)
        if not match:
            raise ValueError("Failed to find stream data in webpage.")

        try:
            data = json.loads(match[1])
        except json.JSONDecodeError:
            raise ValueError(f"Failed to load stream data: {res.text}")

        data = next(iter(data.values()))
        title_data = data["data"]

        if title_data["type"] == "product/movie":
            offer_ids = [x["id"] for x in title_data["relationships"]["offers"]["data"]]
            assets = list(itertools.chain.from_iterable(
                [x["attributes"]["assets"] for x in data["included"] if x["id"] in offer_ids]
            ))
            title_data["assets"] = sorted(assets, key=lambda k: k.get("size", 0))

            return Title(
                id_=self.title,
                type_=Title.Types.MOVIE,
                name=title_data["attributes"]["name"],
                year=int(title_data["attributes"]["releaseDate"][:4]),  # TODO: Find a way to get year
                source=self.ALIASES[0],
                service_data=title_data
            )

        episodes = [
            dict(
                **ep,
                assets=sorted([
                    offer_asset
                    for offer_id in ep["relationships"]["offers"]["data"]
                    for offer_data in [o for o in data["included"] if o["id"] == offer_id["id"]]
                    for offer_asset in offer_data["attributes"]["assets"]
                ], key=lambda o: o.get("size", 0))
            )
            for ep_id in title_data["relationships"]["episodes"]["data"]
            for ep in [e for e in data["included"] if e["id"] == ep_id["id"]]
        ]

        return [Title(
            id_=self.title,
            type_=Title.Types.TV,
            name=title_data["attributes"]["name"],
            season=title_data["attributes"].get("seasonNumber", 0),
            episode=episode["attributes"]["trackNumber"],
            episode_name=episode["attributes"]["name"],
            source=self.ALIASES[0],
            service_data=episode
        ) for episode in episodes]

    def get_tracks(self, title):
        master_hls_url = title.service_data["assets"][-1]["hlsUrl"]
        r = self.session.get(master_hls_url)
        master_hls_manifest = r.text
        master_playlist = m3u8.loads(master_hls_manifest, master_hls_url)

        if self.rentals_supported:
            title_id = title.service_data["id"]
            res = self.session.get(
                url=self.config["endpoints"]["rentals"]
            ).json()
            try:
                self.rental_id = [
                    x for x in res["data"] if x["id"] == title_id
                ][0]["attributes"]["personalizedOffers"][0]["rentalId"]
            except (IndexError, KeyError):
                self.rental_id = None

        tracks = Tracks.from_m3u8(
            master_playlist,
            source=self.ALIASES[0]
        )
        for track in tracks:
            if isinstance(track, VideoTrack):
                track.encrypted = True
            if isinstance(track, AudioTrack):
                track.encrypted = True
                bitrate = re.search(r"(?:_gr|&g=)(\d+?)(?:[&-])", track.extra.uri)
                if bitrate:
                    track.bitrate = int(bitrate[1][-3::]) * 1000  # e.g. 128->128,000, 2448->448,000
                else:
                    raise ValueError(f"Unable to get a bitrate value for Track {track.id}")
                track.codec = track.codec.replace("_ak", "").replace("_ap3", "").replace("_vod", "")
            if isinstance(track, TextTrack):
                track.codec = "vtt"

        tracks.videos = [x for x in tracks.videos if (x.codec or "")[:3] in self.VIDEO_CODEC_MAP[self.vcodec]]
        if not tracks.subtitles:
            for track in tracks.videos:
                track.needs_ccextractor_first = True

        if self.acodec:
            tracks.audios = [
                x for x in tracks.audios if (x.codec or "").split("-")[0] in self.AUDIO_CODEC_MAP[self.acodec]
            ]

        sdh_tracks = [x.language for x in tracks.subtitles if x.sdh]
        tracks.subtitles = [x for x in tracks.subtitles if x.language not in sdh_tracks or x.sdh]

        return Tracks([
            # multiple CDNs, only want one
            x for x in tracks if "ak-amt" in x.url
        ])

    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        return None  # will use common privacy cert

    def license(self, challenge, track, **_):
        data = {
            "streaming-request": {
                "version": 1,
                "streaming-keys": [
                    {
                        "id": 1,
                        "uri": f"data:text/plain;base64,{base64.b64encode(Box.build(track.pssh)).decode()}",
                        "challenge": base64.b64encode(challenge).decode(),
                        "key-system": "com.widevine.alpha",
                        "lease-action": "start",
                    }
                ]
            }
        }

        if self.rental_id:
            data["streaming-request"]["streaming-keys"][0]["rental-id"] = self.rental_id

        res = self.session.post(
            url=self.config["endpoints"]["license"],
            json=data
        ).json()
        status = res["streaming-response"]["streaming-keys"][0]["status"]
        if status != ResponseCode.OK.value:
            self.log.debug(res)
            try:
                desc = ResponseCode(status).name
            except ValueError:
                desc = "UNKNOWN"
            raise self.log.exit(f" - License request failed. Error: {status} ({desc})")
        return res["streaming-response"]["streaming-keys"][0]["license"]

    # Service specific functions

    def configure(self):
        if not re.match(r"https?://(?:geo\.)?itunes\.apple\.com/", self.title):
            raise ValueError("Url must be an iTunes URL...")

        environment = self.get_environment_config()
        if not environment:
            raise self.log.exit("Failed to get iTunes' WEB TV App Environment Configuration...")
        try:
            self.session.headers.update({
                "User-Agent": self.config["user_agent"],
                "Authorization": f"Bearer {environment['MEDIA_API']['token']}",
                "media-user-token": self.session.cookies.get_dict()["media-user-token"],
                "x-apple-music-user-token": self.session.cookies.get_dict()["media-user-token"]
            })
        except KeyError:
            raise self.log.exit(" - No media-user-token cookie found, cannot log in.")
        dsid = self.get_dsid()
        if dsid:
            self.session.headers.update({"X-Dsid": dsid})
            self.rentals_supported = True

    def get_dsid(self):
        data_cache_path = self.get_cache(f"data_{self.profile}.json")
        if os.path.isfile(data_cache_path):
            with open(data_cache_path, encoding="utf-8") as fd:
                icloud = json.load(fd)
            if icloud.get("dsid", None):
                # not expired, lets use
                self.log.info(" + Using cached dsid...")
                return icloud["dsid"]
        # first time login
        self.log.info(" + Logging into iCloud...")
        dsid = self.fetch_dsid()
        if dsid:
            return self.save_dsid(dsid, data_cache_path)
        # unable to fetch dsid, return false
        return None

    @staticmethod
    def save_dsid(dsid, to):
        data = {"dsid": dsid}
        os.makedirs(os.path.dirname(to), exist_ok=True)
        with open(to, "w", encoding="utf-8") as fd:
            json.dump(data, fd)
        return dsid

    def fetch_dsid(self):
        if not self.credentials:
            self.log.warning(" - Credentials are required to download rentals, and none were provided.")
            return None
        res = self.session.post(
            url=self.config["endpoints"]["auth"],
            json={
                "apple_id": self.credentials.username,
                "password": self.credentials.password,
                "extended_login": False
            },
            headers={"Origin": "https://www.icloud.com"}
        ).json()
        if "dsInfo" not in res:
            raise self.log.exit(" - Failed authentication with iCloud for DSID")
        return res["dsInfo"]["dsid"]

    def get_environment_config(self):
        """Loads environment config data from WEB App's <meta> tag."""
        res = self.session.get("https://tv.apple.com").text
        env = re.search(r'web-tv-app/config/environment"[\s\S]*?content="([^"]+)', res)
        if not env:
            return None
        return json.loads(unquote(env[1]))


class ResponseCode(Enum):
    OK = 0
    INVALID_PSSH = -1001
    NOT_OWNED = -1002  # Title not owned in the requested quality
    INSUFFICIENT_SECURITY = -1021  # L1 required or the key used is revoked
