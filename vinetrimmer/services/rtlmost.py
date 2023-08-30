import json
import random
import re
import string

import click
import m3u8
import requests
from langcodes import Language

from vinetrimmer.objects import Title, Tracks
from vinetrimmer.services.BaseService import BaseService
from vinetrimmer.utils import try_get
from vinetrimmer.utils.drmtoday import DRMTODAY_RESPONSE_CODES
from vinetrimmer.utils.regex import find


class RTLMost(BaseService):
    """
    Service code for RTL Most (https://www.rtlmost.hu/).

    \b
    Authorization: Credentials
    Security: UHD@-- HD@L3
    """

    ALIASES = ["RTLM", "rtlmost", "rtlmp"]
    TITLE_RE = r"^(?:https?://(?:www\.)?rtlmost\.hu/)?(?:[a-z0-9-]+-)?(?P<id>[cp]_\d+)"

    @staticmethod
    @click.command(name="RTLMost", short_help="https://rtlmost.hu")
    @click.argument("title", type=str, required=False)
    @click.pass_context
    def cli(ctx, **kwargs):
        return RTLMost(ctx, **kwargs)

    def __init__(self, ctx, title):
        super().__init__(ctx)
        self.parse_title(ctx, title)

        self.configure()

    def get_titles(self):
        m = re.search(r"([cp])_(\d+)$", self.title)
        if not m:
            raise self.log.exit(" - Invalid title ID")

        content_type, title_id = m.groups()

        if content_type == "c":  # clip
            title = self.get_clip_info(title_id)
        else:  # program
            title = self.get_program_info(title_id)

        titles = []

        for clip in title["clips"]:
            season = try_get(clip, lambda x: x["product"]["season"]) or find(r"(\d+)\. évad", clip["title"])
            episode = try_get(clip, lambda x: x["product"]["episode"]) or find(r"(\d+)\. rész", clip["title"])

            if season or episode:
                titles.append(Title(
                    id_=clip["id"],
                    type_=Title.Types.TV,
                    name=clip["program"]["title"],
                    season=season,
                    episode=episode,
                    source=self.ALIASES[0],
                    service_data=clip
                ))
            else:
                titles.append(Title(
                    id_=clip["id"],
                    type_=Title.Types.MOVIE,
                    name=clip["title"],
                    year=clip["product"]["year_copyright"],  # TODO: This seems to be usually/always null
                    source=self.ALIASES[0],
                    service_data=clip
                ))

        return titles

    def get_tracks(self, title):
        assets = title.service_data.get("assets")
        if not assets:
            assets = self.get_clip_info(title.service_data["id"])["clips"][0]["assets"]
        if not assets:
            raise self.log.exit(" - Video not available")

        assets = [x for x in assets if x["type"] in ("usp_hls_h264", "usp_dashcenc_h264")]
        if not assets:
            raise self.log.exit(" - No suitable streams found")

        asset = sorted(assets, key=lambda x: x["video_quality"])[0]  # hd, sd

        manifest_url = asset["full_physical_path"]

        if asset["type"] == "usp_hls_h264":
            # Unencrypted HLS
            tracks = Tracks.from_m3u8(
                master=m3u8.load(manifest_url),
                source=self.ALIASES[0]
            )
        else:
            # DASH CENC
            tracks = Tracks.from_mpd(
                url=manifest_url,
                source=self.ALIASES[0]
            )

        for track in tracks:
            if track.language == Language.get("fr"):
                # TODO: Is there a better way to get the actual language?
                # The service often lies about the audio being French,
                # when it's usually/always Hungarian instead.
                track.language = Language.get("hu")

        return tracks

    def get_chapters(self, title):
        return []  # TODO

    def certificate(self, **_):
        return None  # will use common privacy cert

    def license(self, *, challenge, title, **_):
        self.log.info("Getting JWT")
        res = self.session.get(self.config["endpoints"]["jwt"], headers={
            "x-auth-device-id": self.session.cookies["rtlhuDeviceId"],
            "X-Auth-gigya-signature": self.tokens["UIDSignature"],
            'X-Auth-gigya-signature-timestamp': self.tokens["signatureTimestamp"],
            'X-Auth-gigya-uid': self.tokens["UID"],
            'X-Client-Release': "4.128.7",
            "x-customer-name": "rtlhu"
        }).json()
        jwt = res["token"]

        self.log.info("Getting license request token")
        res = self.session.get(
            self.config["endpoints"]["license_token"].format(uid=self.tokens["UID"], clip_id=title.id),
            headers={
                "Authorization": f"Bearer {jwt}"
            }
        ).json()

        try:
            res = self.session.post(self.config["endpoints"]["license"], headers={
                "x-dt-auth-token": res["token"]
            }, data=challenge).json()
        except requests.HTTPError as e:
            code = e.response.headers.get("x-dt-resp-code")
            if code:
                raise self.log.exit(f" - DRMtoday Error: {DRMTODAY_RESPONSE_CODES.get(code, 'Unknown Error')} ({code})")
        return res["license"]

    # Service-specific functions

    def configure(self):
        # TODO: Cache tokens

        self.log.info(" + Registering device")
        self.session.get(self.config["endpoints"]["device_registration"])

        self.log.info(" + Logging in")
        context_id = f"R{''.join(random.choices(string.digits, k=10))}"
        self.session.post(self.config["endpoints"]["login"], params={
            "context": context_id,
            "saveResponseID": context_id
        }, data={
            "loginID": self.credentials.username,
            "password": self.credentials.password,
            "sessionExpiration": -2,
            "targetEnv": "jssdk",
            "include": "profile,data",
            "includeUserInfo": "true",
            "lang": "hu",
            "APIKey": self.config["api_key"],
            "sdk": "js_latest",
            "authMode": "cookie",
            "pageURL": "https://www.rtlmost.hu/",
            "format": "jsonp",
            "callback": "gigya.callback",
            "context": "R1978336255",
            "utf8": "&#x2713;"
        })

        self.log.info(" + Obtaining auth tokens")
        res = json.loads(self.session.get(self.config["endpoints"]["tokens"], params={
            "APIKey": self.config["api_key"],
            "saveResponseID": context_id,
            "pageURL": "https://www.rtlmost.hu/",
            "noAuth": "true",
            "sdk": "js_latest",
            "format": "jsonp",
            "callback": "gigya.callback",
            "context": context_id
        }).text[15:-2])
        if res["statusCode"] == 200:
            self.tokens = res
        else:
            raise self.log.exit(f"- Failed: {res}")

    def get_clip_info(self, clip_id):
        return self.session.get(self.config["endpoints"]["clip_info"].format(clip_id=clip_id), params={
            "csa": "0",
            "with": "clips,freemiumpacks,program_images,service_display_images,extra_data,program_subcats"
        }, headers={
            "x-6play-freemium": "1",
            "x-auth-device-id": self.session.cookies["rtlhuDeviceId"],
            "x-auth-gigya-signature": self.tokens["UIDSignature"],
            "x-auth-gigya-signature-timestamp": self.tokens["signatureTimestamp"],
            "x-auth-gigya-uid": self.tokens["UID"],
            "x-client-release": "m6group_web-4.128.7",
            "x-customer-name": "rtlhu"
        }).json()

    def get_program_info(self, program_id):
        page = 1
        clips = []

        while True:
            items = self.session.get(self.config["endpoints"]["program_info"].format(program_id=program_id), params={
                "csa": "5",
                "with": "clips,freemiumpacks,expiration",
                "type": "vi,vc,playlist",
                "limit": "100",
                "offset": str((page - 1) * 100)
            }, headers={
                "x-auth-device-id": self.session.cookies["rtlhuDeviceId"],
                "x-auth-gigya-signature": self.tokens["UIDSignature"],
                "x-auth-gigya-signature-timestamp": self.tokens["signatureTimestamp"],
                "x-auth-gigya-uid": self.tokens["UID"],
                "x-client-release": "m6group_web-4.128.7",
                "x-customer-name": "rtlhu"
            }).json()
            for item in items:
                clips.append(item["clips"][0])
            if len(items) < page * 100:
                # last page
                return {"clips": clips}
            page += 1
