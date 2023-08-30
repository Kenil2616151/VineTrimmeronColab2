import base64
import hashlib
import json
import os
import re
import time
from collections import defaultdict
from urllib.parse import urlencode

import click
import jsonpickle
import requests
from langcodes import Language

from vinetrimmer.objects import TextTrack, Title, Tracks
from vinetrimmer.objects.tracks import MenuTrack
from vinetrimmer.services.BaseService import BaseService
from vinetrimmer.utils import try_get
from vinetrimmer.utils.collections import as_list
from vinetrimmer.utils.widevine.device import LocalDevice


class Amazon(BaseService):
    """
    Service code for Amazon VOD (https://amazon.com) and Amazon Prime Video (https://primevideo.com).

    \b
    Authorization: Cookies
    Security: UHD@L1 FHD@L3(ChromeCDM) SD@L3, Maintains their own license server like Netflix, be cautious.

    \b
    Region is chosen automatically based on domain extension found in cookies.
    Prime Video specific code will be run if the ASIN is detected to be a prime video variant.
    """

    ALIASES = ["AMZN", "amazon"]
    TITLE_RE = r"^(?:https?://(?:www\.)?(?P<domain>amazon\.(?P<region>com|co\.uk|de|co\.jp)|primevideo\.com)(?:/.+)?/)?(?P<id>[A-Z0-9]{10,}|amzn1\.dv\.gti\.[a-f0-9-]+)"  # noqa: E501

    AUDIO_CODEC_MAP = {
        "AAC": "mp4a",
        "EC3": "ec-3"
    }

    @staticmethod
    @click.command(name="Amazon", short_help="https://amazon.com, https://primevideo.com")
    @click.argument("title", type=str, required=False)
    @click.option("-b", "--bitrate", default="VBR+CBR",
                  type=click.Choice(["VBR", "CBR", "VBR+CBR"], case_sensitive=False),
                  help="Video Bitrate Mode to download in. VBR=Variable Bitrate, CBR=Constant Bitrate.")
    @click.option("-c", "--cdn", default=None, type=str,
                  help="CDN to download from, defaults to the CDN with the highest weight set by Amazon.")
    # UHD, HD, SD. UHD only returns HEVC, ever, even for <=HD only content
    @click.option("-q", "--quality", default=None,
                  type=click.Choice(["SD", "HD", "UHD"], case_sensitive=False),
                  help="Manifest quality to request.")
    @click.option("-s", "--single", is_flag=True, default=True,
                  help="Force single episode/season instead of getting series ASIN.")
    @click.option("-am", "--audio-manifest", default=None,
                  type=click.Choice(["VBR", "CBR", "H265"], case_sensitive=False),
                  help="Manifest to use for audio. Defaults to H265 if the video manifest is missing 640k audio.")
    @click.option("-aq", "--audio-quality", default="SD",
                  type=click.Choice(["SD", "HD", "UHD"], case_sensitive=False),
                  help="Manifest quality to request for audio. Defaults to the same as --quality.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return Amazon(ctx, **kwargs)

    def __init__(self, ctx, title, bitrate, cdn, quality, single, audio_manifest, audio_quality):
        super().__init__(ctx)
        m = self.parse_title(ctx, title)
        self.domain = m.get("domain")
        self.domain_region = m.get("region")
        self.bitrate = bitrate
        self.cdn = cdn
        self.quality = quality
        self.single = single
        self.audio_manifest = audio_manifest
        self.audio_quality = audio_quality

        self.vcodec = ctx.parent.params["vcodec"]
        self.acodec = ctx.parent.params["acodec"]
        self.range = ctx.parent.params["range_"]
        self.chapters_only = ctx.parent.params["chapters_only"]

        self.cdm = ctx.obj.cdm
        self.profile = ctx.obj.profile

        self.region = {}
        self.endpoints = {}
        self.device = {}

        self.pv = self.domain == "primevideo.com"
        self.device_token = None
        self.device_id = None
        self.customer_id = None
        self.client_id = "f22dbddb-ef2c-48c5-8876-bed0d47594fd"  # browser client id

        quality = ctx.parent.params.get("quality") or 0
        if ((quality == "SD" or quality <= 576) and self.quality is None
                and self.cdm.device.security_level == 3 and self.cdm.device.type == LocalDevice.Types.ANDROID):
            self.log.info("Setting manifest quality to SD for Android L3 (use -q HD to override)")
            self.quality = "SD"

        if quality != "SD" and quality > 1080 and self.quality != "UHD":
            self.log.info("Setting manifest quality to UHD to be able to get 2160p video track")
            self.quality = "UHD"

        if self.quality is None:
            self.quality = "HD"

        self.configure()

    # Abstracted functions

    def get_titles(self):
        if self.pv:
            titles = self.get_titles_prime(self.title)
        else:
            titles = self.get_titles_vod(self.title)
        if titles:
            # TODO: Needs playback permission on first title, title needs to be available
            original_lang = self.get_original_language(self.get_manifest(
                titles[0],
                video_codec="H264",
                bitrate_mode="VBR+CBR",
                quality="HD"
            ))
            if original_lang:
                for title in titles:
                    title.original_lang = Language.get(original_lang)
            else:
                self.log.warning(" - Unable to obtain the title's original language")
        return titles

    def get_tracks(self, title):
        if self.chapters_only:
            return

        manifest = self.get_manifest(
            title,
            video_codec=self.vcodec,
            bitrate_mode=self.bitrate,
            quality=self.quality,
            hdr=self.range
        )

        if "rightsException" in manifest["returnedTitleRendition"]["selectedEntitlement"]:
            self.log.error(" - The profile used does not have the rights to this title.")
            return

        self.customer_id = manifest["returnedTitleRendition"]["selectedEntitlement"]["grantedByCustomerId"]

        default_url_set = manifest["playbackUrls"]["urlSets"][manifest["playbackUrls"]["defaultUrlSetId"]]
        encoding_version = default_url_set["urls"]["manifest"]["encodingVersion"]
        self.log.info(f" + Detected encodingVersion={encoding_version}")

        chosen_manifest = self.choose_manifest(manifest, self.cdn)
        mpd_url = self.clean_mpd_url(chosen_manifest["avUrlInfoList"][0]["url"])
        self.log.debug(mpd_url)

        tracks = Tracks.from_mpd(
            url=mpd_url,
            source=self.ALIASES[0],
            downloader="aria2c",
        )

        need_separate_audio = ((self.audio_quality or self.quality) != self.quality
                               or self.audio_manifest == "VBR" and (self.vcodec, self.bitrate) != ("H264", "VBR")
                               or self.audio_manifest == "CBR" and (self.vcodec, self.bitrate) != ("H264", "CBR")
                               or self.audio_manifest == "H265" and self.vcodec != "H265")

        if not need_separate_audio and (self.vcodec != "H265" or (self.quality == "UHD" and self.range == "HDR10")):
            audios = defaultdict(list)
            for audio in tracks.audios:
                audios[audio.language].append(audio)

            for lang in audios:
                if not any((x.bitrate or 0) >= 640000 for x in audios[lang]):
                    need_separate_audio = True
                    break

        if need_separate_audio:
            manifest_type = self.audio_manifest or "H265"
            self.log.info(
                f"Getting audio from {manifest_type} manifest for potential higher bitrate or better codec"
            )
            audio_manifest = self.get_manifest(
                title,
                "H265" if manifest_type == "H265" else "H264",
                "VBR" if manifest_type != "CBR" else "CBR",
                self.audio_quality or self.quality
            )
            audio_mpd_url = self.clean_mpd_url(self.choose_manifest(
                audio_manifest, self.cdn
            )["avUrlInfoList"][0]["url"])
            self.log.debug(audio_mpd_url)

            try:
                audio_mpd = Tracks.from_mpd(
                    url=audio_mpd_url,
                    source=self.ALIASES[0],
                    downloader="aria2c",
                )
            except KeyError:
                self.log.warning(f" - Title has no {self.audio_manifest} stream, cannot get higher quality audio")
            else:
                tracks.audios = audio_mpd.audios
                self.log.info(" + Done")

        for video in tracks.videos:
            video.hdr10 = chosen_manifest["hdrFormat"] == "Hdr10"
            video.dv = chosen_manifest["hdrFormat"] == "DolbyVision"

        for audio in tracks.audios:
            audio.descriptive = audio.extra[1].get("audioTrackSubtype") == "descriptive"
            # Amazon @lang is just the lang code, no dialect, @audioTrackId has it.
            audio_track_id = audio.extra[1].get("audioTrackId")
            if audio_track_id:
                audio.language = Language.get(audio_track_id.split("_")[0])  # e.g. es-419_ec3_blabla

        if self.acodec:
            tracks.audios = [x for x in tracks.audios if (x.codec or "")[0:4] == self.AUDIO_CODEC_MAP[self.acodec]]

        for sub in manifest.get("subtitleUrls", []) + manifest.get("forcedNarratives", []):
            tracks.add(TextTrack(
                id_=sub.get(
                    "timedTextTrackId",
                    f"{sub['languageCode']}_{sub['type']}_{sub['subtype']}_{sub['index']}"
                ),
                source=self.ALIASES[0],
                url=os.path.splitext(sub["url"])[0] + ".srt",  # DFXP -> SRT forcefully seems to work fine
                # metadata
                codec="srt",  # sub["format"].lower(),
                language=sub["languageCode"],
                forced="forced" in sub["displayName"],
                sdh=sub["type"].lower() == "sdh"  # TODO: what other sub types? cc? forced?
            ), warn_only=True)  # expecting possible dupes, ignore

        return tracks

    def get_chapters(self, title):
        """Get chapters from Amazon's XRay Scenes API."""
        manifest = self.get_manifest(
            title,
            video_codec=self.vcodec,
            bitrate_mode=self.bitrate,
            quality=self.quality,
            hdr=self.range
        )

        if "xrayMetadata" in manifest:
            xray_params = manifest["xrayMetadata"]["parameters"]
        elif self.chapters_only:
            xray_params = {
                "pageId": "fullScreen",
                "pageType": "xray",
                "serviceToken": json.dumps({
                    "consumptionType": "Streaming",
                    "deviceClass": "normal",
                    "playbackMode": "playback",
                    "vcid": manifest["returnedTitleRendition"]["contentId"],
                })
            }
        else:
            return []

        xray_params.update({
            "deviceID": self.device_id,
            "deviceTypeID": self.config["device_types"]["browser"],  # must be browser device type
            "marketplaceID": self.region["marketplace_id"],
            "gascEnabled": str(self.pv).lower(),
            "decorationScheme": "none",
            "version": "inception-v2",
            "uxLocale": "en-US",
            "featureScheme": "XRAY_WEB_2020_V1"
        })

        xray = self.session.get(
            url=self.endpoints["xray"],
            params=xray_params
        ).json().get("page")

        if not xray:
            return []

        widgets = xray["sections"]["center"]["widgets"]["widgetList"]

        scenes = next((x for x in widgets if x["tabType"] == "scenesTab"), None)
        if not scenes:
            return []
        scenes = scenes["widgets"]["widgetList"][0]["items"]["itemList"]

        chapters = []

        for scene in scenes:
            chapter_title = scene["textMap"]["PRIMARY"]
            match = re.search(r"(\d+\. |)(.+)", chapter_title)
            if match:
                chapter_title = match.group(2)
            chapters.append(MenuTrack(
                number=int(scene["id"].replace("/xray/scene/", "")),
                title=chapter_title,
                timecode=scene["textMap"]["TERTIARY"].replace("Starts at ", "")
            ))

        return chapters

    def certificate(self, **_):
        return self.config["certificate"]

    def license(self, challenge, title, **_):
        lic = self.session.post(
            url=self.endpoints["licence"],
            params={
                "asin": title.service_data["titleId"],
                "consumptionType": "Streaming",
                "desiredResources": "Widevine2License",
                "deviceTypeID": self.device["device_type"],
                "deviceID": self.device_id,
                "firmware": 1,
                "gascEnabled": str(self.pv).lower(),
                "marketplaceID": self.region["marketplace_id"],
                "resourceUsage": "ImmediateConsumption",
                "videoMaterialType": "Feature",
                "operatingSystemName": "Linux" if self.quality == "SD" else "Windows",
                "operatingSystemVersion": "unknown" if self.quality == "SD" else "10.0",
                "customerID": self.customer_id,
                "deviceDrmOverride": "CENC",
                "deviceStreamingTechnologyOverride": "DASH",
                "deviceVideoQualityOverride": self.quality,
                "deviceHdrFormatsOverride": self.range
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Bearer {self.device_token}"
            },
            data={
                "widevine2Challenge": base64.b64encode(challenge).decode("utf-8"),  # expects base64
                "includeHdcpTestKeyInLicense": "true"
            }
        ).json()
        if "errorsByResource" in lic:
            error_code = lic["errorsByResource"]["Widevine2License"]
            if "errorCode" in error_code:
                error_code = error_code["errorCode"]
            elif "type" in error_code:
                error_code = error_code["type"]
            message = lic["errorsByResource"]["Widevine2License"]["message"]
            reason = lic["errorsByResource"]["Widevine2License"].get("downstreamReason")
            reason = f" [{reason}]" if reason else ""
            raise self.log.exit(f" - {message} [{error_code}]{reason}")
        return lic["widevine2License"]["license"]

    # Service specific functions

    def configure(self):
        if len(self.title) > 10 and not (self.domain or "").startswith("amazon."):
            self.pv = True

        self.log.info("Getting account region")
        self.region = self.get_region()
        if not self.region:
            raise self.log.exit(" - Failed to get Amazon account region")
        self.GEOFENCE.append(self.region["code"])
        self.log.info(f" + Region: {self.region['code'].upper()}")

        # endpoints must be prepared AFTER region data is retrieved
        self.endpoints = self.prepare_endpoints(self.config["endpoints"], self.region)

        self.session.headers.update({
            "Origin": f"https://{self.region['base']}"
        })

        self.device = (self.config.get("device") or {}).get(self.profile, {})
        if not self.device or self.cdm.device.type == LocalDevice.Types.CHROME or self.quality == "SD":
            # falling back to browser-based device ID
            if not self.device and self.cdm.device.type != LocalDevice.Types.CHROME:
                self.log.warning(
                    f"No device information was provided for profile {self.profile}, using browser device"
                )
            self.device_id = hashlib.sha224(
                ("CustomerID" + self.session.headers["User-Agent"]).encode("utf-8")
            ).hexdigest()
            self.device = {"device_type": self.config["device_types"]["browser"]}
        else:
            device_cache_path = self.get_cache("device_tokens_{profile}_{hash}.json".format(
                profile=self.profile,
                hash=hashlib.md5(json.dumps(self.device).encode()).hexdigest()[0:6]
            ))
            self.device_token = self.DeviceRegistration(
                device=self.device,
                endpoints=self.endpoints,
                cache_path=device_cache_path,
                session=self.session
            ).bearer
            self.device_id = self.device.get("device_serial")
            if not self.device_id:
                raise self.log.exit(f" - A device serial is required in the config, perhaps use: {os.urandom(8).hex()}")
        # prepare range
        self.range = {
            "SDR": "None",
            "HDR10": "Hdr10",
            "DV": "DolbyVision"
        }.get(self.range)
        # prepare bitrate mode
        if self.range == "Hdr10":
            if self.bitrate != "VBR+CBR" and self.quality == "UHD":
                self.log.info(
                    f" + Changed bitrate mode to VBR+CBR as {self.bitrate} returns ISM for UHD HDR manifests"
                )
                self.bitrate = "VBR+CBR"
        else:
            if self.bitrate == "CBR" and self.quality == "UHD":
                self.bitrate = "VBR"
                self.log.info(" + Changed bitrate mode to VBR as CBR returns ISM for UHD manifests")

    def get_titles_prime(self, asin):
        """Get list of Titles for a primevideo.com (Prime) ASIN."""
        res = self.session.get(
            url="https://www.primevideo.com/gp/video/api/getDetailPage",
            params={
                "titleID": asin,
                "isElcano": "1",
                "sections": "Btf"
            },
            headers={
                "Accept": "application/json"
            }
        ).json()["widgets"]
        if res["pageContext"]["subPageType"] == "Movie":
            cards = res["productDetails"]
        else:
            cards = res["titleContent"][0]["cards"]
        cards = [x["detail"] for x in as_list(cards)]
        product_details = res["productDetails"]["detail"]
        return [Title(
            id_=t.get("titleId") or t["catalogId"],
            type_=Title.Types.MOVIE if t["titleType"] == "movie" else Title.Types.TV,
            name=product_details.get("parentTitle") or product_details["title"],
            #year=t.get("releaseYear") or product_details["releaseYear"],
            season=product_details.get("seasonNumber"),
            episode=t.get("episodeNumber"),
            episode_name=t.get("title"),
            source=self.ALIASES[0],
            service_data=dict(**t, titleId=t["catalogId"])
        ) for t in cards]

    def get_titles_vod(self, asin):
        """Get list of Titles for an amazon.com (VOD) ASIN."""
        try:
            res = self.session.get(
                url=self.endpoints["browse"],
                params={
                    "firmware": "1",
                    "deviceTypeID": self.device["device_type"],
                    "deviceID": self.device_id,
                    "format": "json",
                    "version": "2",
                    "formatVersion": "3",
                    "marketplaceId": self.region["marketplace_id"],
                    "IncludeAll": "T",
                    "AID": "T",
                    "SeasonASIN": asin,
                    "Detailed": "T",
                    "tag": "1",
                    "ContentType": "TVEpisode,MOVIE",
                    "IncludeBlackList": "T",
                    "NumberOfResults": "1000",
                    "StartIndex": "0"
                },
                headers={"Accept": "application/json"}
            ).json()["message"]["body"]
        except requests.HTTPError as e:
            if e.response.status_code == 403:
                self.log.debug(e.response.text)
                self.log.exit(" - Unable to get ASIN details. Your cookies may be invalid or have expired. [403]")
            raise

        api_titles = res.get("titles")
        if not api_titles:
            return []

        parent = next((x for x in api_titles[0]["ancestorTitles"] if x["contentType"] == "SERIES"), {})

        if not self.single and parent and parent["titleId"] != asin:
            return self.get_titles_vod(parent["titleId"])

        title_name = parent.get("title")
        if not title_name:
            title_name = api_titles[0]["title"]

        titles = []

        self.log.debug(json.dumps(api_titles, indent=4))

        for t in api_titles:
            if t["contentType"] == "MOVIE":
                titles.append(Title(
                    id_=t["titleId"],
                    type_=Title.Types.MOVIE,
                    name=title_name,
                    #year=(try_get(t, lambda x: x["releaseOrFirstAiringDate"]["valueFormatted"]) or "")[:4],
                    source=self.ALIASES[0],
                    service_data=t,
                ))
            elif t["contentType"] == "EPISODE":
                titles.append(Title(
                    id_=t["titleId"],
                    type_=Title.Types.TV,
                    name=title_name,
                    season=next(
                        (x["number"] for x in t.get("ancestorTitles", []) if x["contentType"] == "SEASON"), None
                    ),
                    episode=t.get("number"),
                    episode_name=t.get("title"),
                    source=self.ALIASES[0],
                    service_data=t,
                ))

        for t in titles:
            if t.season > 100:
                season, volume = divmod(t.season, 100)
                episode = t.episode
                if volume > 1:
                    episode += max([x.episode for x in titles if x.season == t.season - 1] or [0])

                t.season = season
                if t.episode:
                    # Don't modify specials (E00)
                    t.episode = episode

        return titles

    def get_region(self):
        domain_region = self.get_domain_region()
        if not domain_region:
            return {}

        region = self.config["regions"].get(domain_region)
        if not region:
            raise self.log.exit(f" - There's no region configuration data for the region: {domain_region}")

        region["code"] = domain_region

        if self.pv:
            res = self.session.get("https://www.primevideo.com").text
            match = re.search(r'ue_furl *= *([\'"])fls-(na|eu|fe)\.amazon\.[a-z.]+\1', res)
            if match:
                pv_region = match.group(2).lower()
            else:
                raise self.log.exit(" - Failed to get PrimeVideo region")
            pv_region = {"na": "atv-ps"}.get(pv_region, f"atv-ps-{pv_region}")
            region["base_manifest"] = f"{pv_region}.primevideo.com"
            region["base"] = "www.primevideo.com"

        return region

    def get_domain_region(self):
        """Get the region of the cookies from the domain."""
        tld = (self.domain_region or "").split(".")[-1]
        if not tld:
            domains = [x.domain for x in self.cookies if x.domain_specified]
            tld = next((x.split(".")[-1] for x in domains if x.startswith((".amazon.", ".primevideo."))), None)
        return {"com": "us", "uk": "gb"}.get(tld, tld)

    def prepare_endpoint(self, name, uri, region):
        if name in ("browse", "playback", "licence", "xray"):
            return f"https://{(region['base_manifest'])}{uri}"
        if name in ("ontv", "devicelink"):
            return f"https://{region['base']}{uri}"
        if name in ("codepair", "register", "token"):
            return f"https://{self.config['regions']['us']['base_api']}{uri}"
        raise ValueError(f"Unknown endpoint: {name}")

    def prepare_endpoints(self, endpoints, region):
        return {k: self.prepare_endpoint(k, v, region) for k, v in endpoints.items()}

    def choose_manifest(self, manifest, cdn=None):
        """Get manifest URL for the title based on CDN weight (or specified CDN)."""
        if cdn:
            cdn = cdn.lower()
            manifest = next((x for x in manifest["audioVideoUrls"]["avCdnUrlSets"] if x["cdn"].lower() == cdn), {})
            if not manifest:
                raise self.log.exit(f" - There isn't any manifests available on the CDN \"{cdn}\" for this title.")
        else:
            manifest = sorted(manifest["audioVideoUrls"]["avCdnUrlSets"], key=lambda x: int(x["cdnWeightsRank"]))[0]
        return manifest

    def get_manifest(self, title, video_codec, bitrate_mode, quality, hdr=None):
        r = self.session.get(
            url=self.endpoints["playback"],
            params={
                "asin": title.service_data["titleId"],
                "consumptionType": "Streaming",
                "desiredResources": ",".join([
                    "PlaybackUrls",
                    "AudioVideoUrls",
                    "CatalogMetadata",
                    "ForcedNarratives",
                    "SubtitlePresets",
                    "SubtitleUrls",
                    "TransitionTimecodes",
                    "TrickplayUrls",
                    "CuepointPlaylist",
                    "XRayMetadata",
                    "PlaybackSettings"
                ]),
                "deviceID": self.device_id,
                "deviceTypeID": self.device["device_type"],
                "firmware": 1,
                "gascEnabled": str(self.pv).lower(),
                "marketplaceID": self.region["marketplace_id"],
                "resourceUsage": "CacheResources",
                "videoMaterialType": "Feature",
                "playerType": "html5",
                "clientId": self.client_id,
                "operatingSystemName": "Linux" if self.quality == "SD" else "Windows",
                "operatingSystemVersion": "unknown" if self.quality == "SD" else "10.0",
                "deviceDrmOverride": "CENC",
                "deviceStreamingTechnologyOverride": "DASH",
                "deviceProtocolOverride": "Https",
                "deviceVideoCodecOverride": video_codec,
                "deviceBitrateAdaptationsOverride": bitrate_mode.replace("VBR", "CVBR").replace("+", ","),
                "deviceVideoQualityOverride": quality,
                "deviceHdrFormatsOverride": hdr or "None",
                "supportedDRMKeyScheme": "DUAL_KEY",  # ?
                "liveManifestType": "live,accumulating",  # ?
                "titleDecorationScheme": "primary-content",
                "subtitleFormat": "TTMLv2",
                "languageFeature": "MLFv2",  # ?
                "uxLocale": "en_US",
                "xrayDeviceClass": "normal",
                "xrayPlaybackMode": "playback",
                "xrayToken": "XRAY_WEB_2020_V1",
                "playbackSettingsFormatVersion": "1.0.0",
                "playerAttributes": json.dumps({"frameRate": "HFR"}),
                # possibly old/unused/does nothing:
                "audioTrackId": "all"
            },
            headers={"Authorization": f"Bearer {self.device_token}"}
        )
        try:
            manifest = r.json()
        except json.JSONDecodeError:
            self.log.debug(r.text)
            raise self.log.exit(" - Amazon didn't return JSON data when obtaining the playback manifest.")
        if "error" in manifest:
            self.log.debug(manifest["error"])
            raise self.log.exit(" - Amazon reported an error when obtaining the playback manifest.")
        return manifest

    @staticmethod
    def get_original_language(manifest):
        """Get a title's original language from manifest data."""
        try:
            return next(
                x["language"].replace("_", "-")
                for x in manifest["catalogMetadata"]["playback"]["audioTracks"]
                if x["isOriginalLanguage"]
            )
        except (KeyError, StopIteration):
            pass

        if "defaultAudioTrackId" in manifest.get("playbackUrls", {}):
            try:
                return manifest["playbackUrls"]["defaultAudioTrackId"].split("_")[0]
            except IndexError:
                pass

        return None

    @staticmethod
    def clean_mpd_url(mpd_url):
        """Clean up an Amazon MPD manifest url."""
        match = re.match(r"(https?://.*/)d.?/.*~/(.*)", mpd_url)
        if match:
            return "".join(match.groups())
        raise ValueError("Unable to parse MPD URL")

    # Service specific classes

    class DeviceRegistration:

        def __init__(self, device, endpoints, cache_path, session):
            self.session = session
            self.device = device
            self.endpoints = endpoints
            self.cache_path = cache_path

            self.device = {k: str(v) if not isinstance(v, str) else v for k, v in self.device.items()}

            self.bearer = None
            if os.path.isfile(self.cache_path):
                with open(self.cache_path, encoding="utf-8") as fd:
                    cache = jsonpickle.decode(fd.read())
                if cache.get("expires_in", 0) > int(time.time()):
                    # not expired, lets use
                    print("Using cached device bearer...")
                    self.bearer = cache["access_token"]
                else:
                    # expired, refresh
                    print("Cached device bearer expired, refreshing...")
                    refreshed_tokens = self.refresh(self.device, cache["refresh_token"])
                    refreshed_tokens["refresh_token"] = cache["refresh_token"]
                    # expires_in seems to be in minutes, create a unix timestamp and add the minutes in seconds
                    refreshed_tokens["expires_in"] = int(time.time()) + int(refreshed_tokens["expires_in"])
                    with open(self.cache_path, "w", encoding="utf-8") as fd:
                        fd.write(jsonpickle.encode(refreshed_tokens))
                    self.bearer = refreshed_tokens["access_token"]
            else:
                print("Registering new device bearer...")
                self.bearer = self.register(self.device)

        def register(self, device):
            """
            Register device to the account
            :param device: Device data to register
            :return: Device bearer tokens
            """
            # OnTV csrf
            csrf_token = self.get_csrf_token()

            # Code pair
            code_pair = self.get_code_pair(device)

            # Device link
            try:
                self.session.post(
                    url=self.endpoints["devicelink"],
                    headers={
                        "Accept": "*/*",
                        "Accept-Language": "en-US,en;q=0.9,es-US;q=0.8,es;q=0.7",  # needed?
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Referer": self.endpoints["ontv"]
                    },
                    params=urlencode({
                        # any reason it urlencodes here? requests can take a param dict...
                        "ref_": "atv_set_rd_reg",
                        "publicCode": code_pair["public_code"],  # public code pair
                        "token": csrf_token  # csrf token
                    })
                )
            except requests.HTTPError:
                raise ValueError("Unexpected response with the codeBasedLinking request")

            # Register
            try:
                res = self.session.post(
                    url=self.endpoints["register"],
                    headers={
                        "Content-Type": "application/json",
                        "Accept-Language": "en-US"
                    },
                    json={
                        "auth_data": {
                            "code_pair": code_pair
                        },
                        "registration_data": device,
                        "requested_token_type": ["bearer"],
                        "requested_extensions": ["device_info", "customer_info"]
                    },
                    cookies=None  # for some reason, may fail if cookies are present. Odd.
                ).json()
            except requests.HTTPError as e:
                raise ValueError(e.response.text)
            bearer = res["response"]["success"]["tokens"]["bearer"]
            bearer["expires_in"] = int(time.time()) + int(bearer["expires_in"])

            # Cache bearer
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            with open(self.cache_path, "w", encoding="utf-8") as fd:
                fd.write(jsonpickle.encode(bearer))

            return bearer["access_token"]

        def refresh(self, device, refresh_token):
            response = self.session.post(
                url=self.endpoints["token"],
                json={
                    "app_name": device["app_name"],
                    "app_version": device["app_version"],
                    "source_token_type": "refresh_token",
                    "source_token": refresh_token,
                    "requested_token_type": "access_token"
                }
            ).json()
            if "error" in response:
                raise ValueError(
                    f"Failed to refresh device token: {response['error_description']} [{response['error']}]"
                )
            if response["token_type"] != "bearer":
                raise ValueError("Unexpected returned refreshed token type")
            return response

        def get_csrf_token(self):
            """
            On the amazon website, you need a token that is in the html page,
            this token is used to register the device
            :return: OnTV Page's CSRF Token
            """
            res = self.session.get(self.endpoints["ontv"])
            response = res.text
            if 'input type="hidden" name="appAction" value="SIGNIN"' in response:
                raise ValueError(
                    "Cookies are signed out, cannot get ontv CSRF token. "
                    f"Expecting profile to have cookies for: {self.endpoints['ontv']}"
                )
            for match in re.finditer(r"<script type=\"text/template\">(.+)</script>", response):
                prop = json.loads(match.group(1))
                prop = prop.get("props", {}).get("codeEntry", {}).get("token")
                if prop:
                    return prop
            raise ValueError("Unable to get ontv CSRF token")

        def get_code_pair(self, device):
            """
            Getting code pairs based on the device that you are using
            :return: public and private code pairs
            """
            res = self.session.post(
                url=self.endpoints["codepair"],
                headers={
                    "Content-Type": "application/json",
                    "Accept-Language": "en-US"
                },
                json={"code_data": device}
            ).json()
            if "error" in res:
                raise ValueError(f"Unable to get code pair: {res['error_description']} [{res['error']}]")
            return res
