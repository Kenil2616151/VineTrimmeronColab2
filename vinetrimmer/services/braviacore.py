import base64
import hashlib
import json
import os
import sys
from enum import Enum

import click
import jsonpickle
import requests

from vinetrimmer.objects import TextTrack, Title, Tracks, VideoTrack
from vinetrimmer.services.BaseService import BaseService


class BraviaCORE(BaseService):
    """
    Service code for Sony's Bravia CORE streaming service (https://electronics.sony.com/bravia-core).

    \b
    Authorization: Credentials
    Security: UHD@L3 HD@L3

    \b
    Tip: It's currently using unintentionally open internal API endpoints, use while you can!
    """

    ALIASES = ["CORE", "braviacore"]

    @staticmethod
    @click.command(name="BraviaCORE", short_help="https://electronics.sony.com/bravia-core")
    @click.argument("title", type=str, required=False)
    @click.option("-x", "--internal", is_flag=True, default=False,
                  help="Use the weird unintentionally open API endpoint with unrestricted title access.")
    @click.option("-vp", "--vprofile", default=None,
                  type=click.Choice(["h264", "sdr", "hdr", "imax"], case_sensitive=False),
                  help="Video Profile. Default will be highest quality/best compression.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return BraviaCORE(ctx, **kwargs)

    def __init__(self, ctx, title, internal, vprofile):
        super().__init__(ctx)
        self.title = int(title) if title != "list" else title
        self.internal = internal
        self.vprofile = vprofile

        self.session_id = None
        self.credits = None

        self.configure()

    def get_titles(self):
        if self.title == "list":
            if self.internal:
                pages = self.get_cache("manifest_internal")
                if not os.path.isdir(pages) or not os.listdir(pages):
                    raise self.log.exit(
                        " - Endpoint was patched. Can only search cached pages, which you have none of."
                    )
                samples = []
                for page in os.listdir(pages):
                    with open(page, encoding="utf-8") as fd:
                        samples += jsonpickle.decode(fd.read())
                samples = sorted(samples, key=lambda s: int(s["ppId"]))
                for sample in samples:
                    self.log.info(
                        "{} | {} [{}] [{}]".format(
                            sample["ppId"],
                            sample["parent_product_name"],
                            sample["alpha"],
                            sample["quality"]
                        )
                    )
            else:
                self.list_playlist(self.config["playlists"]["unlimited"], "Unlimited Streaming")
                self.list_playlist(self.config["playlists"]["library"], "Library")
            sys.exit(0)

        r = self.session.get(
            url=f"https://service.privilegemovies.com/content/v6/metadata/{self.title}",
            params={"width": "300"}
        )
        title = r.json()
        if title["responseCode"] >= 19999:
            raise ValueError(
                f"Could not get metadata for {self.title}. "
                f"Error: {repr(ResponseCode(title['responseCode']))}. "
                f"URL: {r.request.url}"
            )

        title["id"] = self.title

        search = next((x for x in self.search(title["title"]) if x["parentProductId"] == title["id"]), None)
        if not search:
            raise self.log.exit(f"Could not get search result for {self.title}.")
        title["transactionTypes"] = sorted(search["transactionTypes"])

        return Title(
            id_=title["id"],
            type_=Title.Types.MOVIE,
            name=title["title"],
            year=title["year"],
            original_lang=title["language"],
            source=self.ALIASES[0],
            service_data=title
        )

    def get_tracks(self, title):
        profiles = sorted(
            [x.lower() for x in title.service_data['availableProfiles']],
            key=["imax", "hdr", "sdr", "h264"].index
        )
        profile = None
        if self.vprofile and self.vprofile in profiles:
            profile = self.vprofile.lower()
        elif profiles:
            profile = profiles[0]
        self.log.debug(f"Available Profiles: {profiles}")

        if self.internal:
            res = self.search_manifest_internal(pp_id=title.service_data["id"])
            # disabled for now until true full format is discovered, specifically "s" (signature).
            # res["uri"] = self.prepare_manifest_url(res["uri"], res["movieId"])
        else:
            res = self.get_video(
                title.service_data["id"],
                transaction_type=title.service_data["transactionTypes"][-1],
                profile=profile
            )

        tracks = Tracks.from_mpd(
            data=self.session.get(res["uri"]).text,
            url=res["uri"].replace("service.privilegemovies.com/mg/drm", "cf.privilegemovies.com/drm"),
            source=self.ALIASES[0]
        )

        for sub in res.get("subtitles") or []:
            if sub["extension"] == "vtt":
                continue  # SRT should be available for the exact same sub
            if sub["languageCode"].lower() == "pp":
                sub["languageCode"] = "pt-BR"
            if sub["languageCode"].lower() == "cn":
                sub["languageCode"] = "zh-Hant"
            if sub["languageCode"].lower() == "zh":
                sub["languageCode"] = "zh-Hans"
            try:
                self.session.head(sub["subtitleUrl"])
            except requests.HTTPError as e:
                if e.response.status_code == 404:
                    self.log.warning(f" - Subtitle returned 404, skipping: {sub['subtitleUrl']}")
                    continue
                else:
                    raise
            tracks.add(TextTrack(
                id_="{}_{}_{}_sub".format(
                    self.title,
                    sub["languageCode"],
                    hashlib.md5(sub["subtitleUrl"].encode()).hexdigest()[0:6]
                ),
                source=self.ALIASES[0],
                url=sub["subtitleUrl"],
                # metadata
                codec=sub["extension"],
                language=sub["languageCode"],
                forced=sub["forced"],
                sdh="_CC_" in sub["subtitleUrl"]
            ))

        for track in tracks:
            if isinstance(track, VideoTrack):
                track.hdr10 = profile in ("hdr", "imax")  # TODO: What about DV? Could it be DV?
            track.extra = {"license_url": res["widevineLicenseServer"]}

        return tracks

    def get_chapters(self, title):
        return []

    def certificate(self, **kwargs):
        # TODO: Hardcode the certificate
        return self.license(**kwargs)

    def license(self, challenge, track, **_):
        for n in range(5):
            # even the official APK seems to need to retry at least twice
            res = self.session.post(
                url=track.extra["license_url"],
                data=challenge,  # expects bytes
                # TODO: Need session ID? headers={"Session": self.session_id}
            ).content
            if res and res != b"Unauthorized":
                print(res)
                return res
        raise self.log.exit(" - License API call failed, unable to get certificate or license.")

    # Service specific functions

    def configure(self):
        self.session.headers.update({
            "ApiKey": self.config["api_key"],
            "AppLanguage": "EN"
        })
        self.session_id = self.login()
        self.credits = self.get_available_credits()
        self.log.info(f" - Credits available: {self.credits}.")

    def login(self):
        """
        Log in to BraviaCORE and return a Session ID.
        :returns: Session ID.
        """
        if not self.credentials:
            raise self.log.exit(" - No credentials provided, unable to log in.")
        r = self.session.post(
            url=self.config["endpoints"]["login"],
            json={
                "deviceIdentifier": self.config["device_id"],
                "deviceModel": self.config["device_model"],
                "softwareVersion": self.config["software_version"],
                "email": self.credentials.username,
                "password": self.credentials.password,
            }
        )
        try:
            res = r.json()
        except json.JSONDecodeError:
            raise self.log.exit(f" - Failed to get session ID, response was not JSON: {res.text}")
        if res["responseCode"] >= 19999:
            raise self.log.exit(f" - Failed to log in. Error: {repr(ResponseCode(res['responseCode']))}.")
        return res["session"]

    def get_available_credits(self):
        """Get the amount of available credits in the account."""
        if not self.session_id:
            raise self.log.exit(" - Cannot get available credits, you must log in first")
        r = self.session.get(self.config["endpoints"]["credits"], headers={"Session": self.session_id})
        try:
            res = r.json()
        except json.JSONDecodeError:
            self.log.debug(res.text)
            raise self.log.exit(" - Failed to get available credits, response was not JSON")
        return res["creditsAvailable"]

    def redeem(self, pp_id):
        """Redeem title by Parent Product ID using available credits."""
        if not self.session_id:
            raise self.log.exit(f" - Cannot redeem title {pp_id}, you must log in first")
        definition = "-6"  # TODO: what's the -6? api refers to it as a "definition", seen -2 in v1.1.0 apk
        res = self.session.post(
            url=self.config["endpoints"]["redeem"],
            json={"parentProductIds": f"{pp_id}{definition}"},  # can be multiple values separated by ','.
            headers={"Session": self.session_id}
        ).json()
        res_code = ResponseCode(res["productResponseCodes"][0]["responseCode"])
        if res_code not in [ResponseCode.SUCCESS_REDEEMED, ResponseCode.SUCCESS_ALREAD_REDEEMED]:
            raise self.log.exit(f" - Failed to redeem title {pp_id}{definition}. Error: {repr(res_code)}")
        self.log.info(f" - Redeemed title {pp_id}{definition} [{repr(res_code)}]")

    def get_video(self, pp_id, profile=None, quality=3000, sub_type="srt", is_3d=False, is_4k=True, stream_type=2,
                  transaction_type=1, restrictions_enabled=True):
        """
        Get Video Manifest Information.

        Parameters:
            pp_id: Parent Product ID.
            profile: Profile. imax, hdr, h264, None (best?)
            quality: Quality. 3000, 2160, 1080
            sub_type: Subtitle Format. vtt, srt
            is_3d: Request 3D Video.
            is_4k: Request UHD Video.
            stream_type: ? 2 Seems to be hardcoded.
            transaction_type: ? 1 is typical default.
            restrictions_enabled: ? True Seems to be hardcoded.
        """
        r = self.session.get(
            url=f"https://service.privilegemovies.com/content/v6/video/{pp_id}",
            params={
                "profile": profile,
                "quality": quality,
                "subType": sub_type,
                "is3D": is_3d,
                "is4K": is_4k,
                "streamType": stream_type,
                "transactionType": transaction_type,
                "restrictionsEnabled": restrictions_enabled
            },
            headers={"Session": self.session_id}
        )
        video = r.json()
        if video["responseCode"] > 19999:
            raise ValueError(
                f"Could not get manifest for {pp_id}. "
                f"Error: {repr(ResponseCode(video['responseCode']))}. "
                f"URL: {r.request.url}"
            )
        return dict(
            alpha=video["alpha"],
            audioLanguages=video["audioLanguages"].split(","),
            downloadable=video["downloadable"],
            expiryDate=video["linkExpiry"],
            fairPlayLicenseServer=video["fairPlayLicenseServer"],
            playReadyLicenseServer=video["playReadyLicenseServer"],
            widevineLicenseServer=video["widevineLicenseServer"],
            movieId=video["movieId"],
            trackingId=video["trackingId"],
            tracks=video["productTracks"],
            uri=next((x["url"] for x in video["productTracks"] if x["fileType"] in (20, 11)), None)
        )

    def search_manifest_internal(self, pp_id, movie_id=None):
        """
        Gets all manifest pages from the internal endpoint that was briefly open and returns only wanted title.
        Since it was closed/fixed, only the cached pages are searchable.
        It intentionally gets all manifests before checking for a match to have a safe sorted() check.
        """
        samples = []
        pages = self.get_cache("manifest_internal")
        if os.path.isdir(pages):
            for page in os.listdir(pages):
                with open(page, encoding="utf-8") as fd:
                    samples += [x for x in jsonpickle.decode(fd.read()) if ["ppId"] == pp_id]
        if samples:
            alphas = list(set(x["alpha"].upper() for x in samples))
            if len(alphas) > 1:
                print("Alpha List:")
                for i, a in enumerate(alphas):
                    sub_count = sum(len(x.get('subtitles') or []) for x in samples if x['alpha'].upper() == a)
                    print(f"{i + 1:02}: {a} (Has up to {sub_count} Subtitles)")
                alpha = input("Which alpha (version) do you wish to get? (#): ")
                alpha = alphas[int(alpha or 1) - 1]
            else:
                alpha = alphas[0]
            samples = [x for x in samples if x["alpha"].upper() == alpha.upper()]
            samples = sorted(samples, key=lambda t: int(t["job_number"] or 0))
            samples = sorted(samples, key=lambda t: "SDR" in t["quality"])
            samples = sorted(samples, key=lambda t: "HDR" in t["quality"])
            samples = sorted(samples, key=lambda t: "4K" in t["quality"])
            samples = sorted(samples, key=lambda t: "IMAX" in t["quality"])
            if movie_id:
                samples = sorted(samples, key=lambda t: int(t["movieId"]) == movie_id)
            chosen = samples[-1]
            if not chosen.get("subtitles"):
                chosen["subtitles"] = []
            for sample in samples:
                if sample["alpha"] != chosen["alpha"]:
                    continue
                for subtitle in (sample.get("subtitles") or []):
                    subtitle_data = "".join(reversed(subtitle["subtitleUrl"])).split("_", 1)[-1]
                    if not any([
                        "".join(reversed(x["subtitleUrl"])).split("_", 1)[-1] == subtitle_data
                        for x in chosen["subtitles"]
                    ]):
                        chosen["subtitles"].append(subtitle)
            chosen["widevineLicenseServer"] = chosen["drm_license_url"]
            return chosen
        raise self.log.exit(" - Title was not found in the internal endpoint, possibly in broken pages")

    def get_playlist(self, playlist):
        titles = []
        page = 0
        while True:
            page += 1
            res = self.session.get(
                url=f"https://service.privilegemovies.com/content/v6/playlist/{playlist}/content",
                params={
                    "kids": "false",
                    "width": "300",
                    "PageSize": "48",
                    "PageNumber": str(page)
                }
            ).json()
            res = res["products"]
            titles.extend([Title(
                id_=x["parentProductId"],
                type_=Title.Types.MOVIE if x["contentType"] == 1 else Title.Types.TV,
                name=x["title"],
                year=x["year"],
                season=x.get("season"),
                episode=x.get("episode"),
                episode_name=None,  # TODO: Implement episode_name
                original_lang=x["language"],
                source=self.ALIASES[0],
                service_data=x
            ) for x in res])
            if len(res) < 48:
                break
        return titles

    def list_playlist(self, playlist, name):
        titles = self.get_playlist(playlist)
        self.log.info(f" > {name} ({len(titles)}):")
        for title in titles:
            self.log.info(
                "{} | {} ({}) [{}]".format(
                    title.id,
                    title.name,
                    title.year or "???",
                    ",".join(map(str, title.service_data["transactionTypes"]))
                )
            )

    def search(self, query):
        res = self.session.get(
            url=f"https://service.privilegemovies.com/content/v6/search/{query}",
            params={
                "kids": "false",
                "width": "0"
            }
        ).json()
        return res["results"]

    @staticmethod
    def prepare_manifest_url(url, movie_id):
        if "cf.privilegemovies.com/drm" in url:
            mr = base64.b64encode(json.dumps({
                "v": "7",  # version
                "m": movie_id,  # movie id, title.service_data["parentId"] maybe?
                "u": url,  # original uri
                "minB": "0",  # min bitrate
                "e": "Production",  # environment
                "maxB": "2147483647",  # max bitrate
                "mvas": "false",  # ?
                "al": ["EN", "en-US", "ENG", "UKE", "UKH", "ENH", "ENA", "en-EN"],  # audio languages, what purpose?
                "up": "2021-04-19T16:03:10.373",  # when the file was uploaded
                "o": "cf",  # output, CDN maybe?
                "f": base64.b64encode("-".join([
                    # string format of above?
                    str(movie_id),
                    "manifest.mpd",
                    "0-2147483647",
                    "False",
                    "cf",
                    "637544449903730000",
                    "Production",
                    "7",
                    "EN-en-US-ENG-UKE-UKH-ENH-ENA-en-EN"
                ]).encode()).decode() + ".mpd",
                "s": "CRhH/PTdzH6zzowZu2k3jnRh7zw="  # hmac signature of f?
            }).encode()).decode()
            url = url.replace("cf.privilegemovies.com/drm", "service.privilegemovies.com/mg/drm")
            url += f"?mr={mr}"
        return url


class ResponseCode(Enum):
    ACCEPTANCE_REQUIRED = 40070
    ACCOUNT_EXISTS = 40016
    AGE_NOT_CHECKED = 40015
    AUTO_REDEMPTION_UNAVAILABLE = 40027
    CANNOT_SET_EMPTY_WEBHOOK_URL = 40115
    CANT_DELETE_LAST_CONSUMER_PROFILE = 40092
    CANT_EXPIRE_LAST_PROFILE = 40108
    CANT_MAKE_LAST_PROFILE_KIDS = 40107
    CATEGORY_DEFINITION_ALREADY_EXISTS = 40135
    CATEGORY_DEFINITION_NOT_FOUND = 40134
    CHILD_PRODUCT_NOT_ACTIVE = 40031
    CODE_GENERATION_ERROR = 20006
    CONCURRENT_STREAM_LIMIT_REACHED = 40079
    CONSUMER_BLACK_LISTED = 40047
    CONSUMER_DEVICE_NOT_AUTHENTICATED = 40082
    CONSUMER_NOT_FOUND = 40103
    CONTENT_NOT_RENTED = 40128
    CREDIT_BUNDLE_NOT_FOUND = 40111
    CREDIT_BUNDLE_PRICE_NOT_FOUND = 40113
    DECLINED_PRIVACY_POLICY = 40013
    DECLINED_TERMS_AND_CONDITIONS = 40014
    DEFINITION_REQUIRED = 40065
    DELIVERY_TYPE_NOT_ALLOWED = 40028
    DEVICE_ACTIVATED_TOO_SOON = 40083
    DEVICE_BLACK_LISTED = 40049
    DEVICE_ID_REQUIRED = 40012
    DEVICE_LIMIT_REACHED = 40081
    DEVICE_MEMBERSHIP_NOT_FOUND = 20020
    DEVICE_MODEL_NOT_FOUND = 40140
    DEVICE_MODEL_REQUIRED = 40022
    DEVICE_NO_LONGER_ACTIVE = 40045
    DOWNLOAD_LIMIT_EXCEEDED = 40032
    DOWNLOAD_UNAVAILABLE = 40037
    EMAIL_ALREADY_IN_USE = 40102
    EMAIL_BLACK_LISTED = 40048
    FACEBOOK_LOGIN_DISABLED = 30002
    FACEBOOK_LOGIN_REQUIRED = 40072
    FAILED_TO_BLOCK = 40094
    FAILED_TO_DELETE_BLOCKED = 40095
    FAILED_TO_REDEEM_PRODUCT = 40023
    FAILED_TO_REDEEM_PRODUCT_DEFINITION = 40059
    FAILED_TO_REDEEM_VOUCHER = 20013
    FAILED_TO_REGISTER_DEVICE = 20010
    FAILED_TO_SEND_EMAIL = 20012
    FAILED_TO_UPDATE_DOWNLOAD_STATE = 20011
    FORCED_REDEMPTION_DOES_NOT_EXIST = 40064
    GCM_FAILED_TO_UPDATE_SERVICE = 40074
    GCM_INVALID_INSTANCE_ID = 40073
    GENERIC_NETWORK_ERROR = -1
    INCORRECT_PAYMENT_STATE = 40124
    INSUFFICIENT_PERMISSIONS = 40101
    INVALID_ACCEPTANCE_FORMAT = 40071
    INVALID_ACCESS_TOKEN = 40090
    INVALID_API_KEY = 30001
    INVALID_CHARACTERS_DETECTED = 40093
    INVALID_CONCURRENT_STREAM_EVENT = 40091
    INVALID_CONSUMER_DEVICE = 40080
    INVALID_CONSUMER_PROFILE = 40089
    INVALID_CONTENT_SELECTION_TYPE = 40053
    INVALID_COUNTRY_CODE = 40010
    INVALID_DOWNLOAD_REQUEST_CODE = 40006
    INVALID_EMAIL_FORMAT = 40008
    INVALID_FACEBOOK_TOKEN = 40051
    INVALID_IP_COUNTRY = 40038
    INVALID_IP_FORMAT = 40084
    INVALID_LICENSE_REQUEST_CODE = 40007
    INVALID_NONCE = 40000
    INVALID_PASSWORD = 40003
    INVALID_PASSWORD_FORMAT = 40009
    INVALID_PIN = 40096
    INVALID_PIN_FORMAT = 40097
    INVALID_PLAYSTATION_AUTH_CODE = 40139
    INVALID_PURCHASE_OPTION = 40109
    INVALID_PUSH_NOTIFICATION_DEVICE = 40086
    INVALID_QUALITY = 40060
    INVALID_REDEEM_DEVICE = 40138
    INVALID_REDEMPTION = 40026
    INVALID_SESSION_ID = 40001
    INVALID_SOFTWARE_VERSION = 40046
    INVALID_SPDID = 40041
    INVALID_TEMPORARY_PASSWORD = 40002
    INVALID_URL_PROVIDED = 40117
    INVALID_USERNAME = 40137
    INVALID_USERNAME_OR_PASSWORD = 40005
    INVALID_VOUCHER_CODE = 40004
    IP_BLACK_LISTED = 40050
    MOVIE_CREDIT_REDEMPTION_UNAVAILABLE = 40036
    MOVIE_NOT_FOUND = 40119
    MOVIE_TRACK_NOT_FOUND = 40118
    NOT_ENOUGH_CREDITS = 40021
    NOT_PRIMARY_DEVICE = 40033
    NO_CONSUMER_PREFERENCE_FOUND = 40078
    NO_CONTENT_FOUND = 40025
    NO_CONTENT_PATH = 40039
    NO_EMAIL_ADDRESS_RETRIEVED_FROM_FACEBOOK = 40069
    NO_MOVIES_OF_THE_MONTH_DEFINED = 40133
    NO_PIN_SET = 40098
    NO_STATIC_BANNER_FOUND = 40077
    OUT_DATED_SOFTWARE_VERSION = 40062
    PARENT_PRODUCT_DEFINITION_NOT_REDEEMED = 40061
    PARENT_PRODUCT_DEFINITION_NOT_RENTED = 40131
    PARENT_PRODUCT_DOES_NOT_EXIST = 40058
    PARENT_PRODUCT_EXISTS_IN_PLAYLIST = 40106
    PARENT_PRODUCT_IDS_MISSING = 40063
    PARENT_PRODUCT_ID_REQUIRED = 40068
    PARENT_PRODUCT_NOT_ACTIVE = 40056
    PARENT_PRODUCT_NOT_FOUND = 40110
    PARENT_PRODUCT_NOT_FOUND_IN_PLAYLIST = 40105
    PARENT_PRODUCT_NOT_REDEEMED = 40029
    PARENT_PRODUCT_UNAVAILABLE = 40044
    PASSWORDS_DO_NOT_MATCH = 40024
    PLAYLIST_CUSTOM_LIST_TYPE_NOT_SET = 40132
    PLAYLIST_NOT_FOUND = 40088
    PRODUCT_UNKNOWN_ERROR = 20007
    PROFILE_EXPIRED = 40099
    PROFILE_NAME_EXISTS = 40104
    PROMOTION_UNAVAILABLE = 40035
    PROMO_STATE_NOT_FOUND = 20018
    PURCHASE_LOCATION_REQUIRED = 40030
    REGISTRATION_FAILED = 20009
    RENTAL_PERIOD_ALREADY_EXISTS = 40126
    RENTAL_PERIOD_NOT_FOUND = 40125
    RENTING_DEVICE_LIMIT_REACHED = 40130
    RENTING_NOT_SUPPORTED_BY_WHITE_LABEL_CAMPAIGN = 40129
    REQUIREMENTS_NOT_FOUND = 40085
    SECONDARY_EMAIL_EXISTS = 40100
    SERIES_NOT_FOUND = 40136
    SERVER_ERROR = 20000
    SESSION_ERROR = 20008
    SESSION_EXPIRED = 40011
    SOFTWARE_VERSION_NOT_FOUND = 20019
    SPDID_EXPIRED = 40042
    SPDID_NO_SETUP = 20015
    SPDID_RAW_DEVICE_INVALID = 40043
    SPDID_REQUIRED = 40040
    SUBSCRIPTION_ALREADY_CANCELLED = 40127
    SUBSCRIPTION_INVOICE_NOT_FOUND = 40123
    SUBSCRIPTION_NOT_FOUND = 40122
    SUBSCRIPTION_PLAN_NOT_FOUND = 40121
    SUBTITLE_NOT_FOUND = 40120
    SUCCESS = 10000
    SUCCESS_ACCEPTANCE_REQUIRED = 10008
    SUCCESS_ALREAD_REDEEMED = 10002
    SUCCESS_END = 19999
    SUCCESS_FAILED_EMAIL = 10005
    SUCCESS_FAILED_REDEEMED = 10007
    SUCCESS_INVALID_LANGUAGE = 10001
    SUCCESS_INVALID_VOUCHER = 10004
    SUCCESS_OK_ALREADY_REDEEMED_VOUCHER_CODE = 10011
    SUCCESS_OK_ALREADY_RENTING = 10013
    SUCCESS_OK_INVALID_NOTIFICATION_MESSAGE = 10010
    SUCCESS_OK_SOME_FAILED = 10012
    SUCCESS_REDEEMED = 10006
    SUCCESS_TEMPORARY_PASSWORD_USED = 10003
    TEAM_NOT_FOUND = 40114
    THIRD_PARTY_INACTIVE = 30000
    TIER_PRICE_NOT_FOUND = 40112
    UNKNOWN_CAMPAIN_GROUP_ERROR = 20002
    UNKNOWN_CONSUMER_ERROR = 20004
    UNKNOWN_DEVICE_ERROR = 20003
    UNKNOWN_SESSION_ERROR = 20005
    UNKNOWN_SESSION_ERROR_2 = 20014
    UNKNOWN_THEME_ERROR = 20016
    UNKNOWN_WHITE_LABEL_CAMPAIGN_ERROR = 20017
    UNKNOWN_WHITE_LABEL_ERROR = 20001
    VIDEO_UNLIMITED_NOT_SUPPORTED = 30003
    VOUCHER_BUNDLE_NOT_ACTIVE = 40057
    VOUCHER_BUNDLE_NOT_STARTED = 40075
    VOUCHER_CODE_EXPIRED = 40019
    VOUCHER_CODE_HASNT_STARTED = 40055
    VOUCHER_CODE_INVALID = 40018
    VOUCHER_CODE_INVALID_COUNTRY = 40054
    VOUCHER_CODE_INVALID_USER_TYPE = 40052
    VOUCHER_CODE_REQUIRED = 40017
    VOUCHER_CODE_USED = 40020
    VOUCHER_CODE_WRONG_PROMO = 40076
    VOUCHER_REDEMPTION_UNAVAILABLE = 40034
    VOUCHER_RULE_INVALID_DEVICE = 40067
    WEBHOOK_EVENT_NOT_FOUND = 40116
    WHITE_LABEL_CAMPAIGN_DOWNLOAD_DISABLED = 40066
    WHITE_LABEL_CAMPAIGN_NOT_FOUND = 40087
