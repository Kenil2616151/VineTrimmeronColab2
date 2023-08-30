import base64
import datetime
import hashlib
import hmac
import re
import urllib.parse

import click
import requests

from vinetrimmer.objects import TextTrack, Title, Tracks
from vinetrimmer.services.BaseService import BaseService


class RakutenTV(BaseService):
    """
    Service code for Rakuten's Rakuten TV streaming service (https://rakuten.tv).

    \b
    Authorization: Credentials
    Security: UHD@L3, doesn't care about releases.

    \b
    TODO: - TV Shows are not yet supported as there's 0 TV Shows to purchase, rent, or watch in my region
          - Due to unpopularity or need of use, this hasn't been getting regular updates so the codebase or
            any API changes may have broken this codebase; It needs testing

    \b
    NOTES: - Only movies are supported as my region's Rakuten has no TV shows available to purchase at all
           - Some values are hardcoded and need to be manually configured
           - All configuration exists in this file and need to be moved to a config file
           - The Manifest MPD is parsed by youtube-dl which picks what it thinks is the best copy
    """

    ALIASES = ["RKTN", "rakutentv"]
    TITLE_RE = r"^(?:https?://(?:www\.)?rakuten\.tv/movies(?:/[a-z]{2})?/)(?P<id>[a-z0-9-]+)"

    @staticmethod
    @click.command(name="RakutenTV", short_help="https://rakuten.tv")
    @click.argument("title", type=str, required=False)
    @click.option("-m", "--movie", is_flag=True, default=False, help="Title is a movie.")
    @click.option("--vquality", default="UHD", type=click.Choice(["SD", "HD", "FHD", "UHD"], case_sensitive=False),
                  help="Video Quality.")
    @click.option("--achannels", default="atmos", type=click.Choice(["2.0", "5.1", "atmos"], case_sensitive=False),
                  help="Audio Channels.")
    @click.option("--market", default="ie", type=click.Choice(["ie", "uk"], case_sensitive=False),
                  help="Market Code.")
    @click.option("--locale", default="en-IE", type=click.Choice(["en-IE", "en-GB"], case_sensitive=False),
                  help="Locale Code.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return RakutenTV(ctx, **kwargs)

    def __init__(self, ctx, title, movie, vquality, achannels, market, locale):
        super().__init__(ctx)
        self.parse_title(ctx, title)
        self.movie = movie
        self.vquality = vquality
        self.achannels = achannels
        self.market = market
        self.locale = locale

        self.range = ctx.parent.params["range_"]

        self.app_version = "3.7.3b"
        self.classification_id = 41
        self.device_identifier = "android"  # web, android, andtv?
        self.device_serial = "6cc3584a-c182-4cc1-9f8d-b90e4ed76de9"
        self.access_token = None
        self.session_uuid = None
        self.player = "andtv:DASH-CENC:WVM"  # web: FHD, android: SD, andtv: 4k
        self.license_url = None

        self.configure()

    def get_titles(self):
        self.login_android()  # web: https://rakuten.tv/api/login different format, will need its own method
        title_url = f"https://gizmo.rakuten.tv/v3/movies/{self.title}?" + urllib.parse.urlencode({
            "classification_id": self.classification_id,
            "device_identifier": self.device_identifier,
            "device_serial": self.device_serial,
            "locale": self.locale,
            "market_code": self.market,
            "session_uuid": self.session_uuid,
            "timestamp": f"{int(datetime.datetime.now().timestamp())}005"
        })
        # TODO: for some reason if I include the full `&signature=` it fails
        title_url += "signature=" + self.generate_signature(title_url)
        title = self.session.get(url=title_url).json()
        if "errors" in title:
            error = title["errors"][0]
            if error["code"] == "error.not_found":
                raise self.log.exit(f"Title [{self.title}] was not found on this account.")
            else:
                raise self.log.exit(f"Unable to get title info: {error['message']} [{error['code']}]")
        title = title["data"]

        if self.movie:
            return Title(
                id_=self.title,
                type_=Title.Types.MOVIE,
                name=title["title"],
                year=title.get("year"),
                source=self.ALIASES[0],
                service_data=title
            )
        else:
            raise self.log.exit(" - TV shows are not yet supported")

    def get_tracks(self, title):
        # TODO: These values are presumed (seems last item may be best) and arent
        #       correct. Depending on device data, it will provide based on that
        #       so I need to provide an L1 capable devices contents to it to get
        #       correct data, so for now, the values are hard coded and can be
        #       changed at the top of the script.
        # self.video_quality = title["labels"]["video_qualities"][-1]["id"]
        # self.audio_quality = title["labels"]["audio_qualities"][-1]["id"]
        # self.hdr_type = title["labels"]["hdr_types"][-1]["id"]
        stream_info_url = "https://gizmo.rakuten.tv/v3/me/streamings?" + urllib.parse.urlencode({
            "device_stream_video_quality": self.vquality,
            "device_identifier": self.device_identifier,
            "market_code": self.market,
            "session_uuid": self.session_uuid,
            "timestamp": f"{int(datetime.datetime.now().timestamp())}122"
        })
        stream_info_url += "signature=" + self.generate_signature(stream_info_url)
        stream_info = self.session.post(
            url=stream_info_url,
            data={
                "hdr_type": {"SDR": "NONE", "HDR10": "HDR10", "DV": "DOLBY_VISION"}.get(self.range),
                "audio_quality": self.achannels,  # TODO: don't presume
                "app_version": self.app_version,
                "content_id": self.title,
                "video_quality": self.vquality,
                "audio_language": "ENG",  # TODO: don't presume
                "video_type": "stream",
                "device_serial": self.device_serial,
                "content_type": "movies" if self.movie else "episodes",
                "classification_id": self.classification_id,
                "subtitle_language": "MIS",
                "player": self.player
            }
        ).json()
        if "errors" in stream_info:
            error = stream_info["errors"][0]
            raise self.log.exit(f" - Failed to get track info: {error['message']} [{error['code']}]")
        stream_info = stream_info["data"]["stream_infos"][0]

        self.license_url = stream_info["license_url"]

        tracks = Tracks.from_mpd(
            url=stream_info["url"],
            session=self.session,
            source=self.ALIASES[0]
        )

        for sub in stream_info.get("all_subtitles") or []:
            if sub["type"] in ["Subtitles-Burned"]:
                # for some reason there's a pseudo sub track when there's subs burned into the video
                continue
            tracks.add(TextTrack(
                id_=hashlib.md5(sub["url"].encode()).hexdigest()[0:6],
                source=self.ALIASES[0],
                url=sub["url"],
                # metadata
                codec="srt",
                language=sub["locale"],  # sub['locale'] and/or sub['language'] might be an uppercase alpha 3
            ))

        return tracks

    def get_chapters(self, title):
        return []

    def certificate(self, **kwargs):
        # TODO: Hardcode the certificate
        return self.license(**kwargs)

    def license(self, challenge, **_):
        return self.session.post(
            url=self.license_url,
            data=challenge  # expects bytes
        ).content

    # Service specific functions

    def configure(self):
        self.session.headers.update({
            "Origin": f"https://rakuten.tv/{self.market}",
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 9; SM-G950F Build/PPR1.180610.011)"
        })

    def generate_signature(self, url):
        up = urllib.parse.urlparse(url)
        msg = re.sub(r"&timestamp=\d+", "", up.query)
        digester = hmac.new(self.access_token.encode(), f"GET{up.path}{msg}".encode(), hashlib.sha1)
        return base64.b64encode(digester.digest()).decode("utf-8").replace("+", "-").replace("/", "_")

    def login_android(self):
        # TODO: Make this return the tokens, move print out of the func
        print("Logging into RakutenTV as an Android device")
        if not self.credentials:
            raise self.log.exit(" - No credentials provided, unable to log in.")
        try:
            res = self.session.post(
                # web: https://rakuten.tv/api/login
                url="https://gizmo.rakuten.tv/v3/me/login_or_wuaki_link",
                params={
                    "device_identifier": self.device_identifier,
                    "market_code": self.market
                },
                data={
                    "app_version": self.app_version,
                    "device_metadata[uid]": self.device_serial,
                    "device_metadata[os]": "Android",
                    "device_metadata[model]": "SM-G950F",
                    "device_metadata[year]": 2019,
                    "device_serial": self.device_serial,
                    "device_metadata[trusted_uid]": True,
                    "device_metadata[brand]": "samsung",
                    "classification_id": self.classification_id,
                    "user[password]": self.credentials.password,
                    "device_metadata[app_version]": self.app_version,
                    "user[username]": self.credentials.username,
                    "device_metadata[serial_number]": self.device_serial
                }
            ).json()
        except requests.HTTPError as e:
            if e.response.status_code == 403:
                raise self.log.exit(
                    " - Rakuten returned a 403 (FORBIDDEN) error. "
                    "This could be caused by your IP being detected as a proxy, or regional issues. Cannot continue."
                )
        if "errors" in res:
            error = res["errors"][0]
            raise self.log.exit(f" - Login failed: {error['message']} [{error['code']}]")
        self.access_token = res["data"]["user"]["access_token"]
        self.session_uuid = res["data"]["user"]["session_uuid"]
        self.classification_id = res["data"]["user"]["profile"]["classification"]["id"]
