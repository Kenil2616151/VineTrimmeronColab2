import hashlib
import json

import click
import langcodes

from vinetrimmer.objects import TextTrack, Title, Tracks
from vinetrimmer.services.BaseService import BaseService


class Boomerang(BaseService):
    """
    Service code for Boomerang (https://www.boomerang.com).

    \b
    Authorization: Credentials
    Security: UHD@??, HD@L3

    \b
    Tips:
    - The API returns the incorrect year for most titles, use TVDB/IMDB instead.
    - Streams were found stored as "_SDR_AVC", couldn't find any HDR, might exist?

    - The title ID can be fetched on the player page:
        - For TV shows: /watch/<title id>/<episode id>
        - Same as above for movies, a movie is considered E01.
    """

    ALIASES = ["BOOM", "boomerang"]

    @staticmethod
    @click.command(name="Boomerang", short_help="https://boomerang.com")
    @click.argument("title", type=int)
    @click.option("-m", "--movie", is_flag=True, default=False, help="Title is a movie.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return Boomerang(ctx, **kwargs)

    def __init__(self, ctx, title, movie):
        super().__init__(ctx)
        self.title = title
        self.movie = movie

        self.configure()

    def get_titles(self):
        self.log.info(" + Getting metadata")
        metadata = self.get_metadata()

        page = 1
        titles = []

        while True:
            r = self.session.get(self.config["endpoints"]["episodes"].format(title_id=self.title), params={
                "page": page,
                "page_size": "25",
                "trans": "en",
            })

            try:
                res = r.json()
            except json.JSONDecodeError:
                raise self.log.exit(f" - Failed to get episode data: {r.text}")

            if self.movie:
                titles += [Title(
                    id_=self.title,
                    type_=Title.Types.MOVIE,
                    name=title["series_title"],
                    year=metadata["year"],
                    original_lang=metadata["language"],
                    source=self.ALIASES[0],
                    service_data=title,
                ) for title in res["values"]]
            else:
                titles += [Title(
                    id_=metadata["name"],
                    type_=Title.Types.TV,
                    name=title["series_title"],
                    season=title["season"],
                    episode=title["season_position"],
                    episode_name=title["title"],
                    original_lang=metadata["language"],
                    source=self.ALIASES[0],
                    service_data=title,
                ) for title in res["values"]]

            if page >= res["num_pages"]:
                break

            page += 1

        return titles

    def get_tracks(self, title):
        self.session.headers["x-auth-jwt"] = self.session.get(
            self.config["endpoints"]["jwt"].format(uuid=title.service_data["uuid"]),
            params={
                "page": "1",
                "page_size": "25",
                "trans": "en",
            },
        ).text.strip("'")

        r = self.session.get(self.config["endpoints"]["manifest"], params={
            "cdn": "cloudfront",
            "drm": "widevine",
            "st": "dash",
            "subs": "none",
            "trans": "en",
        })

        try:
            res = r.json()
        except json.JSONDecodeError:
            raise self.log.exit(f" - Failed to get manifest: {r.text}")

        tracks = Tracks.from_mpd(
            url=res["stream_url"],
            session=self.session,
            source=self.ALIASES[0],
        )

        r = self.session.get(
            self.config["endpoints"]["subtitles"].format(guid=title.service_data["guid"]),
            params={"trans": "en"},
        )

        try:
            res = r.json()
        except json.JSONDecodeError:
            raise self.log.exit(f" - Failed to get subtitles: {r.text}")

        tracks.add(TextTrack(
            id_=hashlib.md5(res[0]["url"].encode()).hexdigest()[0:6],
            source=self.ALIASES[0],
            url=res[0]["url"],
            codec="vtt",
            language=res[0]["code"],
            forced=False,
            sdh=False,
        ))

        return tracks

    def get_chapters(self, title):
        return []

    def certificate(self, **kwargs):
        # TODO: Hardcode the certificate
        return self.license(**kwargs)

    def license(self, challenge, **_):
        return self.session.post(self.config["endpoints"]["license"], data=challenge).content

    # Service-specific functions

    def get_consumer_secret(self):
        """
        Generates and returns a new consumer secret from the API.
        A consumer secret is used in the x-consumer-key header
        to authenticate all subsequent requests.
        """
        r = self.session.get(self.config["endpoints"]["consumer"])

        try:
            return r.json()["consumer_secret"]
        except KeyError:
            raise self.log.exit(f" - Failed to log in: {r.text}")

    def login(self):
        """
        Creates a new authentication session and generates and returns
        a new authentication token from the API. An authentication token
        is used in the authorization header to further authenticate
        all subsequent requests.
        """
        if not self.credentials:
            raise self.log.exit(" - No credentials provided, unable to log in.")

        r = self.session.post(self.config["endpoints"]["login"], data={
            "username": self.credentials.username,
            "password": self.credentials.password
        })

        try:
            return r.json()["result"]["session_id"]
        except KeyError:
            raise self.log.exit(f" - Failed to log in: {r.text}")

    def get_metadata(self):
        """
        Fetches and returns metadata (title name, original broadcast language
        and release year) from the API for the specified title ID.
        """
        r = self.session.get(self.config["endpoints"]["metadata"].format(title_id=self.title))

        try:
            res = r.json()
        except json.JSONDecodeError:
            self.log.exit(f" - Failed to get series data: {r.text}")

        return {
            "name": res["title"],
            "language": langcodes.find(res["broadcast_language_name"]).language,
            "year": res["year"],
        }

    def configure(self):
        self.log.info(" + Logging in")
        self.session.headers.update({
            "x-consumer-key": self.get_consumer_secret(),
            "authorization": f"Token {self.login()}",
        })
