import base64
import json
import os
from http.cookiejar import MozillaCookieJar

import click
import requests

from vinetrimmer.config import directories
from vinetrimmer.objects import Title, Tracks
from vinetrimmer.services.BaseService import BaseService


class Showtime(BaseService):
    """
    Service code for Showtime (https://www.showtime.com/).

    \b
    Authorization: Credentials or Cookies
    Security: UHD@L3
    """

    ALIASES = ["SHO", "showtime"]
    #GEOFENCE = ["us"]
    TITLE_RE = r"^(?:https?://(?:www\.)?showtime\.com(?:/#)?/(?P<type>movie|series|episode|play)/)?(?P<id>\d+)"

    VIDEO_RANGE_MAP = {
        "DV": "DOLBY_VISION"
    }

    AUDIO_CODEC_MAP = {
        "AAC": "mp4a",
        "AC3": "ac-3",
        "EC3": "ec-3"
    }

    @staticmethod
    @click.command(name="Showtime", short_help="https://showtime.com")
    @click.argument("title", type=str, required=False)
    @click.option("-m", "--movie", is_flag=True, default=False, help="Title is a movie.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return Showtime(ctx, **kwargs)

    def __init__(self, ctx, title, movie):
        super().__init__(ctx)
        m = self.parse_title(ctx, title)
        self.movie = movie or m.get("type") == "movie"

        self.profile = ctx.obj.profile

        self.range = ctx.parent.params["range_"]
        self.vcodec = ctx.parent.params["vcodec"]
        self.acodec = ctx.parent.params["acodec"]

        quality = ctx.parent.params.get("quality") or 0
        if quality != "SD" and quality > 1080 and self.vcodec != "H265":
            self.log.info(" + Switched video codec to H265 to be able to get 2160p video track")
            self.vcodec = "H265"

        if self.range in ("HDR10", "DV") and self.vcodec != "H265":
            self.log.info(f" + Switched video codec to H265 to be able to get {self.range} dynamic range")
            self.vcodec = "H265"

        self.configure()

    def get_titles(self):
        res = self.session.get(
            self.config["endpoints"]["metadata"]["movie" if self.movie else "series"].format(title_id=self.title)
        ).json()

        self.log.debug(json.dumps(res, indent=4))
        if "error" in res:
            raise self.log_exit(res)

        if self.movie:
            return Title(
                id_=res["id"],
                type_=Title.Types.MOVIE,
                name=res["name"],
                year=res["releaseYear"],
                source=self.ALIASES[0],
                service_data=res,
            )
        else:
            return [Title(
                id_=ep["id"],
                type_=Title.Types.TV,
                name=ep["series"]["seriesTitle"],
                season=ep["series"]["seasonNum"],
                episode=ep["series"]["episodeNum"],
                episode_name=ep["name"],
                source=self.ALIASES[0],
                service_data=ep,
            ) for ep in res["episodesForSeries"]]

    def get_tracks(self, title):
        res = self.start_play(title)

        tracks = Tracks.from_mpd(
            data=self.session.get(res["uri"]).text,
            url=res["uri"],
            source=self.ALIASES[0]
        )

        for track in tracks:
            track.needs_proxy = True

        if self.acodec:
            tracks.audios = [x for x in tracks.audios if (x.codec or "")[:4] == self.AUDIO_CODEC_MAP[self.acodec]]

        # Filter out false positives that actually seem to be video(?)
        tracks.subtitles = [x for x in tracks.subtitles if x.codec and "mp4" not in x.codec]

        # Needed to avoid 3 simultaneous video streams reached error
        # TODO: Only call this after license (but we need to call it even if we used cached keys)
        try:
            self.session.get(
                self.config["endpoints"]["endplay"].format(title_id=title.id, at=res["at"])
            )
        except requests.HTTPError:
            self.log.warning(
                " - Failed to send endplay request, this may result in a too many concurrent streams error."
            )

        title.service_data = res

        return tracks

    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        return None  # will use common privacy cert

    def license(self, *, challenge, title, retrying=False, **kwargs):
        r = self.session.post(self.config["endpoints"]["license"], params={
            "refid": title.service_data["refid"],
            "authToken": base64.b64encode(title.service_data["entitlement"].encode()),
        }, data=challenge, headers={
            "X-STAT-videoQuality": self.VIDEO_RANGE_MAP.get(self.range, self.range)
        })

        try:
            res = r.json()
        except json.JSONDecodeError:
            # Not valid JSON, so probably an actual license
            return r.content

        if res["error"]["code"] == "widevine.auth" and not retrying:
            self.log.warning(" - Auth token expired, refreshing...")
            title.service_data = self.start_play(title)
            return self.license(challenge=challenge, title=title, retrying=True, **kwargs)

        raise self.log_exit(res)

    # Service specific functions

    def configure(self):
        self.session.headers.update({
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 7.1.2; AFTMM Build/NS6271)",
            "X-STAT-model": "Sparrow",
            "X-STAT-displayType": "TV",
            "X-STAT-appVersion": "1.11",
            "X-STAT-contentVersion": "OTT"
        })

        if self.vcodec == "H265":
            self.session.headers.update({
                "X-STAT-resolution": "4K"
            })

        cookie_file = os.path.join(directories.cookies, self.__class__.__name__.lower(), f"{self.profile}.txt")
        cookie_jar = MozillaCookieJar(cookie_file)

        if os.path.isfile(cookie_file):
            cookie_jar.load()
            if any(x.name == "JSESSIONID" for x in cookie_jar):
                self.session.cookies.update(cookie_jar)
                self.log.info(" + Using saved cookies")
                return
            self.log.warning(" - Cookies expired, logging in again")

        self.log.info(" + Logging in")
        if not (self.credentials and self.credentials.username and self.credentials.password):
            raise self.log.exit(" - No credentials provided, unable to log in.")

        res = self.session.post("https://www.showtime.com/api/user/login", json={
            "email": self.credentials.username,
            "password": self.credentials.password
        }).json()
        self.log.debug(res)

        if "error" in res:
            if res["error"]["code"] == "error.invalid.email":
                raise self.log.exit(
                    " - Invalid email. "
                    "(If your email is valid, logins from your IP may have been blocked temporarily.)"
                )
            else:
                raise self.log_exit(res)

        for cookie in self.session.cookies:
            cookie_jar.set_cookie(cookie)
        os.makedirs(os.path.dirname(cookie_file), exist_ok=True)
        cookie_jar.save()

    def start_play(self, title):
        res = self.session.get(
            self.config["endpoints"]["startplay"].format(title_id=title.id),
            headers={
                "X-STAT-videoQuality": self.VIDEO_RANGE_MAP.get(self.range, self.range)
            }
        ).json()
        self.log.debug(json.dumps(res, indent=4))

        if "error" in res:
            raise self.log_exit(res)

        return res

    def log_exit(self, res):
        raise self.log.exit(f" - {res['error']['title']} - {res['error']['body']} [{res['error']['code']}]")
