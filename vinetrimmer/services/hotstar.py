import base64
import hashlib
import hmac
import json
import os
import time
import uuid
import requests
from urllib.parse import urlparse, parse_qs
from urllib.request import urlopen, Request

import click

from vinetrimmer.objects import Title, Tracks
from vinetrimmer.services.BaseService import BaseService


class Hotstar(BaseService):
    """
    Service code for Star India's Hotstar (aka Disney+ Hotstar) streaming service (https://hotstar.com).

    \b
    Authorization: Credentials
    Security: UHD@L3, doesn't seem to care about releases.

    \b
    Tips: - The library of contents can be viewed without logging in at https://hotstar.com
          - The homepage hosts domestic programming; Disney+ content is at https://hotstar.com/in/disneyplus
    """

    ALIASES = ["HS", "hotstar"]
    #GEOFENCE = ["in"]
    TITLE_RE = r"^(?:https?://(?:www\.)?hotstar\.com/[a-z0-9/-]+/)(?P<id>\d+)"

    @staticmethod
    @click.command(name="Hotstar", short_help="https://hotstar.com")
    @click.argument("title", type=str, required=False)
    @click.option("-m", "--movie", is_flag=True, default=False, help="Title is a movie.")
    @click.option("-q", "--quality", default="fhd",
                  type=click.Choice(["4k", "fhd", "hd", "sd"], case_sensitive=False),
                  help="Manifest quality to request.")
    @click.option("-c", "--channels", default="5.1", type=click.Choice(["5.1", "2.0", "atmos"], case_sensitive=False),
                  help="Audio Codec")
    @click.pass_context
    def cli(ctx, **kwargs):
        return Hotstar(ctx, **kwargs)

    def __init__(self, ctx, title, movie, quality, channels):
        super().__init__(ctx)
        self.parse_title(ctx, title)
        self.movie = movie
        self.quality = quality
        self.channels = channels

        assert ctx.parent is not None

        self.vcodec = ctx.parent.params["vcodec"]
        self.acodec = ctx.parent.params["acodec"] or "EC3"
        self.range = ctx.parent.params["range_"]

        self.profile = ctx.obj.profile

        self.device_id = None
        self.hotstar_auth = None
        self.token = None
        self.license_api = None

        self.configure()

    def get_titles(self):
        headers = {
            "Accept": "*/*",
            "Accept-Language": "en-GB,en;q=0.5",
            "hotstarauth": self.hotstar_auth,
            "X-HS-UserToken": self.token,
            "X-HS-Platform": self.config["device"]["platform"]["name"],
            "X-HS-AppVersion": self.config["device"]["platform"]["version"],
            "X-Country-Code": "in",
            "x-platform-code": "PCTV"
        }
        r = self.session.get(
            url=self.config["endpoints"]["movie_title"] if self.movie else self.config["endpoints"]["tv_title"],
            headers=headers,
            params={"contentId": self.title}
        )
        try:
            res = r.json()["body"]["results"]["item"]
        except json.JSONDecodeError:
            raise ValueError(f"Failed to load title manifest: {res.text}")

        if res["assetType"] == "MOVIE":
            return Title(
                id_=self.title,
                type_=Title.Types.MOVIE,
                name=res["title"],
                year=res["year"],
                original_lang=res["langObjs"][0]["iso3code"],
                source=self.ALIASES[0],
                service_data=res,
            )
        else:
            r = self.session.get(
                url=self.config["endpoints"]["tv_episodes"],
                headers=headers,
                params={
                    "eid": res["id"],
                    "etid": "2",
                    "tao": "0",
                    "tas": "1000"
                }
            )
            try:
                res = r.json()["body"]["results"]["assets"]["items"]
            except json.JSONDecodeError:
                raise ValueError(f"Failed to load episodes list: {r.text}")
            return [Title(
                id_=self.title,
                type_=Title.Types.TV,
                name=x.get("showShortTitle"),
                year=x.get("year"),
                season=x.get("seasonNo"),
                episode=x.get("episodeNo"),
                episode_name=x.get("title"),
                original_lang=x["langObjs"][0]["iso3code"],
                source=self.ALIASES[0],
                service_data=x
            ) for x in res]

    def get_tracks(self, title):
        akamai_cdn=True
        while akamai_cdn:
            r = self.session.post(
                url=self.config["endpoints"]["manifest"].format(id=title.service_data["contentId"]),
                params={
                    # TODO: Perhaps set up desired-config to actual desired playback set values?
                    "desired-config": "|".join([
                        "audio_channel:stereo",
                        "dynamic_range:hdr10",
                        "encryption:widevine",
                        "ladder:tv",
                        "package:dash",
                        "resolution:fhd",
                        "video_codec:h264"
                    ]),
                    "device-id": self.device_id,
                },
                headers={
                    "Accept": "*/*",
                    "hotstarauth": self.hotstar_auth,
                    "x-hs-usertoken": self.token,
                    "x-hs-request-id": self.device_id,
                    "x-country-code": "in"
                },
                json={
                    "os_name": "Windows",
                    "os_version": "10",
                    "app_name": "web",
                    "app_version": "7.34.1",
                    "platform": "Chrome",
                    "platform_version": "99.0.4844.82",
                    "client_capabilities": {
                        "ads": ["non_ssai"],
                        "audio_channel": ["stereo"],
                        "dvr": ["short"],
                        "package": ["dash", "hls"],
                        "dynamic_range": ["sdr"],
                        "video_codec": ["h264"],
                        "encryption": ["widevine"],
                        "ladder": ["tv"],
                        "container": ["fmp4"],
                        "resolution": ["hd"]
                    },
                    "drm_parameters": {
                        "widevine_security_level": ["SW_SECURE_DECODE", "SW_SECURE_CRYPTO"],
                        "hdcp_version": ["HDCP_V2_2", "HDCP_V2_1", "HDCP_V2", "HDCP_V1"]
                    },
                    "resolution": "auto"
                }
            )
            try:
                playback_sets = r.json()["data"]["playback_sets"]
            except json.JSONDecodeError:
                raise ValueError(f"Manifest fetch failed: {r.text}")

            # transform tagsCombination into `tags` key-value dictionary for easier usage
            playback_sets = [dict(
                **x,
                tags=dict(y.split(":") for y in x["tags_combination"].lower().split(";"))
            ) for x in playback_sets]

            playback_set = next((
                x for x in playback_sets
                if x["tags"].get("encryption") == "widevine" or x["tags"].get("encryption") == "plain" # widevine, fairplay, playready
                if x["tags"].get("package") == "dash"  # dash, hls
                if x["tags"].get("container") == "fmp4br" # fmp4, fmp4br, ts
                if x["tags"].get("ladder") == "tv"  # tv, phone
                if x["tags"].get("video_codec").endswith(self.vcodec.lower())  # dvh265, h265, h264 - vp9?
                # user defined, may not be available in the tags list:
                if x["tags"].get("resolution") in [self.quality, None]  # max is fine, -q can choose lower if wanted
                if x["tags"].get("dynamic_range") in [self.range.lower(), None]  # dv, hdr10, sdr - hdr10+?
                if x["tags"].get("audio_codec") in [self.acodec.lower(), None]  # ec3, aac - atmos?
                if x["tags"].get("audio_channel") in [{"5.1": "dolby51", "2.0": "stereo", "atmos": "atmos"}[self.channels], None]
            ), None)
            if not playback_set:
                playback_set = next((
                    x for x in playback_sets
                    if x["tags"].get("encryption") == "widevine" or x["tags"].get("encryption") == "plain" # widevine, fairplay, playready
                    if x["tags"].get("package") == "dash"  # dash, hls
                    if x["tags"].get("ladder") == "tv"  # tv, phone
                    if x["tags"].get("resolution") in [self.quality, None]
                    ), None)
            if not playback_set:
                raise ValueError("Wanted playback set is unavailable for this title...")
            if "licence_url" in playback_set: self.license_api = playback_set["licence_url"]
            if playback_set['token_algorithm'] == 'AKAMAI-HMAC':
                akamai_cdn = False

        r = Request(playback_set["playback_url"])
        r.add_header("user-agent", "Hotstar;in.startv.hotstar/3.3.0 (Android/8.1.0)")
        data = urlopen(r).read()

        mpd_url = playback_set["playback_url"].replace(".hotstar.com", ".akamaized.net")

        tracks = Tracks.from_mpd(
            url=mpd_url,
            data=data,
            session=self.session,
            source=self.ALIASES[0]
        )
        for track in tracks:
            track.needs_proxy = True
        return tracks

    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        return None  # will use common privacy cert

    def license(self, challenge, **_):
        return self.session.post(
            url=self.license_api,
            data=challenge  # expects bytes
        ).content

    # Service specific functions

    def configure(self):
        self.session.headers.update({
            "Origin": "https://www.hotstar.com",
            "Referer": "https://www.hotstar.com/in"
        })
        self.log.info("Logging into Hotstar")
        self.hotstar_auth = self.get_akamai()
        self.log.info(f" + Calculated HotstarAuth: {self.hotstar_auth}")
        if self.cookies:
            self.device_id = self.session.cookies.get("device_id")
            self.log.info(f" + Using Device ID: {self.device_id}")
        else:
            self.device_id = str(uuid.uuid4())
            self.log.info(f" + Created Device ID: {self.device_id}")
        self.token = self.get_token()
        print("Obtained tokens")

    @staticmethod
    def get_akamai():
        enc_key = b"\x05\xfc\x1a\x01\xca\xc9\x4b\xc4\x12\xfc\x53\x12\x07\x75\xf9\xee"
        st = int(time.time())
        exp = st + 6000
        res = f"st={st}~exp={exp}~acl=/*"
        res += "~hmac=" + hmac.new(enc_key, res.encode(), hashlib.sha256).hexdigest()
        return res

    def get_token(self):
        token_cache_path = self.get_cache("token_{profile}.json".format(profile=self.profile))
        if os.path.isfile(token_cache_path):
            with open(token_cache_path, encoding="utf-8") as fd:
                token = json.load(fd)
            if token.get("exp", 0) > int(time.time()):
                # not expired, lets use
                self.log.info(" + Using cached auth tokens...")
                return token["uid"]
            # expired, refresh
            self.log.info(" + Refreshing and using cached auth tokens...")
            return self.save_token(self.refresh(token["uid"], token["sub"]["deviceId"]), token_cache_path)
        # get new token
        if self.cookies:
            token = self.session.cookies.get("userUP", None, 'www.hotstar.com', '/' + 'in')
        else:
            token = self.login()
        return self.save_token(token, token_cache_path)

    @staticmethod
    def save_token(token, to):
        # Decode the JWT data component
        data = json.loads(base64.b64decode(token.split(".")[1] + "===").decode("utf-8"))
        data["uid"] = token
        data["sub"] = json.loads(data["sub"])

        os.makedirs(os.path.dirname(to), exist_ok=True)
        with open(to, "w", encoding="utf-8") as fd:
            json.dump(data, fd)

        return token

    def refresh(self, user_id_token, device_id):
        r = self.session.get(
            url=self.config["endpoints"]["refresh"],
            headers={
                    "x-hs-usertoken": user_id_token,
                    "hotstarauth": self.hotstar_auth,
                    "x-hs-device-id": device_id,
                    "X-HS-Platform": self.config["device"]["platform"]["name"]
            }
        )
        try:
            res = r.json()
        except json.JSONDecodeError:
            raise self.log.exit(f" - Failed to refresh token, response was not JSON: {r.text}")
        if "errorCode" in res:
            raise self.log.exit(f" - Token Refresh failed: {res['description']} [{res['errorCode']}]")
        return res["user_identity"]

    def login(self):
        """
        Log in to HOTSTAR and return a JWT User Identity token.
        :returns: JWT User Identity token.
        """
        if self.credentials.username == "username" and self.credentials.password == "password":
            logincode_url = "https://api.hotstar.com/in/aadhar/v2/firetv/in/users/logincode/"
            logincode_headers = {
                    "Content-Length": "0",
                    "User-Agent": "Hotstar;in.startv.hotstar/3.3.0 (Android/8.1.0)"
            }
            logincode = self.session.post(
                url = logincode_url,
                headers = logincode_headers
            ).json()["description"]["code"]
            print(f"Go to tv.hotstar.com and put {logincode}")
            logincode_choice = input('Did you put as informed above? (y/n): ')
            if logincode_choice.lower() == 'y':
                res = self.session.get(
                    url = logincode_url+logincode,
                    headers = logincode_headers
                )
            else:
                self.log.exit(" - Exited.")
                raise
        else:
            res = self.session.post(
                url=self.config["endpoints"]["login"],
                json={
                    "isProfileRequired": "false",
                    "userData": {
                        "deviceId": self.device_id,
                        "password": self.credentials.password,
                        "username": self.credentials.username,
                        "usertype": "email"
                    },
                    "verification": {}
                },
                headers={
                    "hotstarauth": self.hotstar_auth,
                    "content-type": "application/json"
                }
            )
        try:
            data = res.json()
        except json.JSONDecodeError:
            self.log.exit(f" - Failed to get auth token, response was not JSON: {res.text}")
            raise
        if "errorCode" in data:
            self.log.exit(f" - Login failed: {data['description']} [{data['errorCode']}]")
            raise
        return data["description"]["userIdentity"]
