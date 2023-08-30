import json

import click
import requests
from langcodes import Language

from vinetrimmer.objects import Title, Tracks
from vinetrimmer.services.BaseService import BaseService
from vinetrimmer.utils import Cdm
from vinetrimmer.utils.regex import find
from vinetrimmer.vendor.pymp4.parser import Box


class AXNPlayer(BaseService):
    """
    Service code for AXN Player (https://hu.axn.com/axn-player/).

    \b
    Authorization: None
    Security: HD@L3
    """

    ALIASES = ["AXNP", "axnplayer", "axn"]
    GEOFENCE = ["hu"]
    TITLE_RE = r"^(?:https?://hu\.axn\.com/axn-player/.+/)?(?P<id>[a-z0-9-]+)"

    @staticmethod
    @click.command(name="AXNPlayer", short_help="https://hu.axn.com/axn-player")
    @click.argument("title", type=str, required=False)
    @click.pass_context
    def cli(ctx, **kwargs):
        return AXNPlayer(ctx, **kwargs)

    def __init__(self, ctx, title):
        super().__init__(ctx)
        self.parse_title(ctx, title)

    def get_titles(self):
        # TODO: Support full shows and parse season/episode number

        r = self.session.get(self.config["endpoints"]["watch"].format(title_id=self.title))

        player_url = json.loads(find(r'"https:\\/\\/player\.zype\.com[^"]+"', r.text))
        if not player_url:
            raise self.log.exit(" - Unable to extract player URL")

        r = self.session.get(player_url)

        return Title(
            id_=self.title,
            type_=Title.Types.MOVIE,
            name=find(r"video_title: '(.+)'", r.text),
            source=self.ALIASES[0],
            service_data=r.text,
        )

    def get_tracks(self, title):
        manifest_url = find(r"'(http[^']+manifest\.mpd)'", title.service_data)
        if not manifest_url:
            raise self.log.exit(" - Unable to extract manifest URL")

        tracks = Tracks.from_mpd(
            url=manifest_url,
            source=self.ALIASES[0],
        )

        for track in tracks:
            # Service gets wrong PSSH by default, need to build it manually
            if track.encrypted:
                track.get_kid()
                track.pssh = Box.parse(Box.build(dict(
                    type=b"pssh",
                    version=0,
                    flags=0,
                    system_ID=Cdm.uuid,
                    init_data=b"\x12\x10" + bytes.fromhex(track.kid),
                )))

            # Service lies about language
            # TODO: Is there any content that's actually English?
            if track.language == Language.get("en"):
                track.language = Language.get("hu")

        return tracks

    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        return self.config["certificate"]

    def license(self, *, challenge, title, **_):
        custom_data = find(r"customdata: '([^']+)'", title.service_data)

        try:
            return self.session.post(self.config["endpoints"]["license"], data=challenge, headers={
                "customdata": custom_data,
            }).content
        except requests.HTTPError as e:
            print(e.request.headers['customdata'])
            raise self.log.exit(f" - Failed to get license: {e.response.json()['message']}")

    # Service-specific functions

    def configure(self):
        self.session.headers.update({
            "origin": "https://hu.axn.com",
            "referer": "https://hu.axn.com/",
        })
