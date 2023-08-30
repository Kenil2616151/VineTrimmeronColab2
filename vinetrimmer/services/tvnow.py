import base64
import json

import click
import requests
from bs4 import BeautifulSoup
from langcodes import Language

from vinetrimmer.objects import Title, Tracks
from vinetrimmer.services.BaseService import BaseService
from vinetrimmer.utils.regex import find


class TVNOW(BaseService):
    """
    Service code for RTL Germany's TVNOW (https://www.tvnow.de/).

    \b
    Authorization: Cookies
    Security: UHD@-- FHD@L3

    Requires an EU IP, and they block servers/VPNs for license requests.
    """

    ALIASES = ["TVNOW"]
    TITLE_RE = r"^(?:https?://(?:www\.)?tvnow\.de/(?:filme|serien|shows)/[a-z0-9-]+-)?(?P<id>\d+)"

    @staticmethod
    @click.command(name="TVNOW", short_help="https://tvnow.de")
    @click.argument("title", type=str, required=False)
    @click.option("--api-season", is_flag=True, default=False,
                  help="Always use season numbers from the API instead of scraping the website."
                       "This means always using the year as season for annual format shows.")
    @click.option("--no-filter", is_flag=True, default=False,
                  help="Don't filter seasons before scraping. This is slower but may fix issues with -w.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return TVNOW(ctx, **kwargs)

    def __init__(self, ctx, title, api_season, no_filter):
        super().__init__(ctx)
        self.parse_title(ctx, title)
        self.api_season = api_season
        self.no_filter = no_filter

        self.wanted = ctx.parent.params["wanted"]

        self.profile = ctx.obj.profile

        self.configure()

    def configure(self):
        self.session.headers.update({
            "origin": "https://www.tvnow.de",
            "referer": "https://www.tvnow.de/",
            "X-Auth-Token": self.session.cookies["jwt"],
            "X-Now-Logged-In": "1"
        })

    def get_titles(self):
        try:
            res = self.session.get(self.config["endpoints"]["title_info_web"].format(title_id=self.title)).json()
        except requests.HTTPError as e:
            if e.response.status_code == 401:
                self.log.exit(" - HTTP Error 401: Unauthorized. Cookies may be expired.")
            raise
        self.log.debug(json.dumps(res, indent=4))

        content_type = next(iter(res["seo"]["jsonLd"]))

        # TODO: Find a way to replace HTML parsing for original language
        soup = BeautifulSoup(res["seo"]["text"], "lxml-html")

        try:
            original_lang = Language.find((
                soup.find(string="Originalsprache (OV)")
                or soup.find(string="Originalsprache")
            ).find_next("li").text)
        except AttributeError:
            self.log.warning(" - Unable to obtain the title's original language")
            original_lang = None

        if content_type == "movie":
            user_info = json.loads(base64.b64decode(self.session.cookies["jwt"].split(".")[1] + "=="))

            res2 = self.session.get(self.config["endpoints"]["title_info_firetv"].format(title_id=self.title), headers={
                "X-Pay-Type": "premium" if user_info["permissions"]["vodPremium"] else "free",
                "X-GOOGLE-ID": "231080e6-9b21-4ee8-9ca4-5ca7d395d962",
                "X-CLIENT-VERSION": "400000",
                "X-TRANSFORMSCOPE": "fire",
                "transformscope": "fire",
                "X-DEVICE-TYPE": "tv",
                "X-Bff-Api-Version": "1",
                "User-Agent": "okhttp/4.9.0"
            }).json()
            self.log.debug(json.dumps(res2, indent=4))

            return Title(
                id_=next(x["id"] for x in res["modules"] if x["type"] == "player"),
                type_=Title.Types.MOVIE,
                name=res["seo"]["jsonLd"]["movie"]["name"],
                year=res2["teaser"]["video"]["productionYear"],
                original_lang=original_lang,
                source=self.ALIASES[0]
            )
        elif content_type == "series":
            res = self.session.get(self.config["endpoints"]["navigation"].format(title_id=self.title)).json()
            self.log.debug(res)

            annual = res["moduleLayout"] == "format_annual_navigation"

            if annual:
                seasons = []
                for x in res["items"]:
                    for y in x["months"]:
                        seasons.append((x["year"], y["month"]))
            else:
                seasons = [x["season"] for x in res.get("items") or []]

            titles = []

            for season in seasons:
                if annual:
                    if self.wanted and not (self.no_filter or any(x.startswith(f"{season[0]}x") for x in self.wanted)):
                        continue

                    year, month = season
                    res = self.session.get(
                        self.config["endpoints"]["episodes_annual"].format(title_id=self.title, year=year, month=month)
                    ).json()
                else:
                    if self.wanted and not (self.no_filter or any(x.startswith(f"{season}x") for x in self.wanted)):
                        continue

                    res = self.session.get(
                        self.config["endpoints"]["episodes"].format(title_id=self.title, season=season)
                    ).json()
                self.log.debug(json.dumps(res, indent=4))

                if annual:
                    season = season[0]

                titles += [Title(
                    id_=ep["videoId"],
                    type_=Title.Types.TV,
                    name=ep["ecommerce"]["teaserFormatName"],
                    season=season if self.api_season else find(
                        r"^Staffel (\d+)$", ep["ecommerce"].get("teaserSeason", "")
                    ),
                    episode=find(r"^Folge (\d+)$", ep["ecommerce"].get("teaserEpisodeNumber", "")),
                    episode_name=ep["ecommerce"].get("teaserEpisodeName") or ep["headline"],
                    original_lang=original_lang,
                    source=self.ALIASES[0]
                ) for ep in res["items"]]

            return titles
        else:
            raise self.log.exit(f" - Unsupported content type: {content_type}")

    def get_tracks(self, title):
        res = self.session.get(self.config["endpoints"]["player"].format(title_id=title.id)).json()
        self.log.debug(json.dumps(res, indent=4))

        if "errorType" in res:
            if res["errorType"] == "premium":
                raise self.log.exit(" - This title requires a premium subscription.")
            else:
                raise self.log.exit(f" - Failed to get manifest [{res['errorType']}]")

        return Tracks.from_mpd(
            url=res["videoConfig"]["videoSource"]["streams"]["dashHdUrl"],
            source=self.ALIASES[0]
        )

    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        return None  # will use common privacy cert

    def license(self, challenge, **_):
        try:
            r = self.session.post(self.config["endpoints"]["license"], data=challenge)
        except requests.HTTPError as e:
            try:
                res = e.response.json()
            except json.JSONDecodeError:
                # Not valid JSON, so probably an actual license
                raise e

            self.log.debug(res)

            error = res["error"]
            if error == "forbidden.by.businessrule":
                error += ". This may mean that your IP is blocked."
            raise self.log.exit(f" - Failed to get license: {error}")

        return r.content
