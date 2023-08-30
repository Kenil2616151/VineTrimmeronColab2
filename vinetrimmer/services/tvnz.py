import base64
import json
import os
import time
from urllib.parse import urljoin

import click

from vinetrimmer.objects import MenuTrack, Title, Track, Tracks
from vinetrimmer.services.BaseService import BaseService
from vinetrimmer.utils import try_get
from vinetrimmer.utils.regex import find


class TVNZ(BaseService):
    """
    Service code for TVNZ (https://www.tvnz.co.nz/).

    \b
    Authorization: Credentials
    Security: HD@L3
    """

    ALIASES = ["TVNZ"]
    #GEOFENCE = ["nz"]
    TITLE_RE = r"^(?:https?://(?:www\.)?tvnz\.co\.nz/shows/)?(?P<id>[a-z0-9-]+)"

    @staticmethod
    @click.command(name="TVNZ", short_help="https://tvnz.co.nz")
    @click.argument("title", type=str, required=False)
    @click.pass_context
    def cli(ctx, **kwargs):
        return TVNZ(ctx, **kwargs)

    def __init__(self, ctx, title):
        super().__init__(ctx)
        self.parse_title(ctx, title)

        self.profile = ctx.obj.profile

        self.wanted = ctx.parent.params["wanted"]

        self.access_token = None

        self.configure()

    def get_titles(self):
        res = self.session.get(self.config["endpoints"]["shows"].format(title_id=self.title)).json()
        self.log.debug(json.dumps(res, indent=4))

        pages = []

        is_movie = "genre:movie" in res["metadata"]["keywords"]

        for season in res["layout"]["defaultSectionLayout"]["slots"]["main"]["modules"][0]["lists"]:
            if not is_movie:
                season_num = int(season["label"].split()[-1].rstrip("B"))
                if self.wanted and not any(x.startswith(f"{season_num}x") for x in self.wanted):
                    continue

            page = self.session.get(urljoin(self.config["endpoints"]["base"], season["href"])).json()
            self.log.debug(json.dumps(page, indent=4))
            pages.append(page)

            while True:
                if not page["nextPage"]:
                    break

                if self.wanted and not any(x for x in self.wanted if not any(
                    y for y in page["_embedded"].values()
                    if y["seasonNumber"].rstrip("B") == x.split("x")[0]
                    and y["episodeNumber"] == x.split("x")[1]
                )):
                    # Don't fetch further pages if we already have all wanted episodes.
                    # NOTE: This may cause an inaccurate total episode count if -w is used.
                    break

                page = self.session.get(urljoin(self.config["endpoints"]["base"], page["nextPage"])).json()
                self.log.debug(json.dumps(page, indent=4))
                pages.append(page)

        titles = []
        for page in pages:
            titles += [Title(
                id_=ep["id"].split("/")[-1],
                type_=Title.Types.MOVIE if is_movie else Title.Types.TV,
                name=ep["title"] if is_movie else res["structuredData"][0]["name"],
                season=int(ep["seasonNumber"].rstrip("B")),
                episode=int(ep["episodeNumber"]),
                episode_name=(
                    ep["title"] if ep["title"] != try_get(res, lambda x: x["structuredData"][0]["name"]) else None
                ),
                source=self.ALIASES[0],
                service_data=ep,
            ) for ep in page["_embedded"].values()]

        for title in titles:
            if title.service_data.get("seasonNumber").endswith("B"):
                title.episode += max(
                    x.episode for x in titles if str(title.season) == x.service_data["seasonNumber"]
                )

        return titles

    def get_tracks(self, title):
        self.configure()  # Refresh token if necessary

        res = self.session.get(
            self.config["endpoints"]["play"].format(title_id=self.title, season=title.season, episode=title.episode),
            headers={
                "Authorization": f"Bearer {self.access_token}",
            },
        ).json()
        self.log.debug(json.dumps(res, indent=4))

        playback = next(x for x in res["_embedded"].values() if x["type"] == "playback")
        source = next(x for x in playback["sources"] if x["mediaType"]["subtype"] == "dash+xml")

        title.service_data["chapters"] = source["cuepoints"]
        title.service_data["license_url"] = source["drm"]["com.widevine.alpha"]

        tracks = Tracks.from_mpd(
            url=source["src"],
            source=self.ALIASES[0],
        )

        for track in tracks:
            track.needs_proxy = True

            if track.language.territory == "NZ":
                track.language.territory = None

        return tracks

    def get_chapters(self, title):
        return [MenuTrack(
            number=i + 1,
            title="Credits" if chapter["type"] == "CREDITS" else f"Chapter {(i + 1):02}",
            timecode=MenuTrack.format_duration(Track.pt_to_sec(chapter["time"])),
        ) for i, chapter in enumerate(title.service_data["chapters"])]

    def certificate(self, **_):
        return None  # will use common privacy cert

    def license(self, *, challenge, title, **_):
        return self.session.post(title.service_data["license_url"], data=challenge).content

    # Service-specific functions

    def configure(self):
        self.session.headers.update({
            "origin": self.config["endpoints"]["base"],
            "referer": f"{self.config['endpoints']['base']}/",
        })

        cache_path = self.get_cache(f"tokens_{self.profile}.json")
        if os.path.isfile(cache_path):
            self.log.info("Using cached token")

            with open(cache_path, encoding="utf-8") as fd:
                self.access_token = json.load(fd)["access_token"]

            data = json.loads(base64.b64decode(self.access_token.split(".")[1] + "=="))
            if data["exp"] <= int(time.time()):
                self.log.warning(" - Token expired, logging in again")
                self.access_token = None

        if not self.access_token:
            self.log.info("Logging in")

            res = self.session.post(self.config["endpoints"]["login"], json={
                "client_id": self.config["client_id"],
                "credential_type": "password",
                "password": self.credentials.password,
                "username": self.credentials.username,
            }, headers={
                "auth0_client": base64.b64encode(
                    json.dumps(self.config["auth0_client"], separators=(",", ":")).encode()
                ).decode(),
                "origin": "https://login.tech.tvnz.co.nz",
                "referer": "https://login.tech.tvnz.co.nz/",
            }).json()
            self.log.debug(json.dumps(res, indent=4))

            r = self.session.get(self.config["endpoints"]["authorize"], params={
                "client_id": self.config["client_id"],
                "response_type": "token id_token",
                "audience": "tvnz-apis",
                "connection": "tvnz-users",
                "redirect_uri": "https://login.tech.tvnz.co.nz/callback/login",
                "state": base64.b64encode(os.urandom(24)).decode(),
                "nonce": base64.b64encode(os.urandom(24)).decode(),
                "login_ticket": res["login_ticket"],
                "scope": "openid profile email",
                "auth0Client": base64.b64encode(
                    json.dumps(self.config["auth0_client"], separators=(",", ":")).encode()
                ).decode(),
            }, allow_redirects=False)
            self.log.debug(r.text)
            self.access_token = find(r"access_token=([^&]+)", r.text)

            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as fd:
                json.dump({"access_token": self.access_token}, fd)
