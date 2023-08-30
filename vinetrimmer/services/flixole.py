import json

import click

from vinetrimmer.objects import Title, Tracks
from vinetrimmer.services.BaseService import BaseService


class FlixOle(BaseService):
    """
    Service code for FlixOlé streaming service (https://ver.flixole.com/).

    \b
    Authorization: Credentials
    Security: HD@L3, doesn't care about releases.
    """

    ALIASES = ["FO", "flixole"]
    TITLE_RE = r"^(?:https?://ver\.flixole\.com/watch/)?(?P<id>[a-f0-9-]+)"

    @staticmethod
    @click.command(name="FlixOle", short_help="https://flixole.com")
    @click.argument("title", type=str, required=False)
    @click.pass_context
    def cli(ctx, **kwargs):
        return FlixOle(ctx, **kwargs)

    def __init__(self, ctx, title):
        super().__init__(ctx)
        self.parse_title(ctx, title)

        self.login_data = {}
        self.entitlement_id = None
        self.license_headers = None

        self.configure()

    def get_titles(self):
        r = self.session.post(
            self.config["endpoints"]["title"],
            json={
                "variables": {
                    "viewableId": f"{self.title}",
                    "broadcastId": "",
                },
                "query": """
                    query viewable($viewableId: ID!) {
                        viewer {
                            id: magineId
                            viewable(magineId: $viewableId) {
                              __typename
                              id: magineId
                              title
                              description
                              ...MovieFragment
                            }
                          }
                        }

                        fragment MovieFragment on Movie {
                          title
                          banner: image(type: \"sixteen-nine\")
                          poster: image(type: \"poster\")
                          metaImage: image(type: \"poster\")
                          description
                          duration
                          durationHuman
                          genres
                          productionYear
                          inMyList
                          trailer
                          entitlement {
                            ...EntitlementFragment
                          }
                          defaultPlayable {
                            ...PlayableFragment
                          }
                          providedBy {
                            brand
                          }
                          webview
                        }

                        fragment EntitlementFragment on EntitlementInterfaceType {
                          __typename
                          offer {
                            ...OfferFragment
                          }
                          purchasedAt
                          ... on EntitlementRentType {
                            entitledUntil
                          }
                          ... on EntitlementPassType {
                            entitledUntil
                          }
                        }

                        fragment OfferFragment on OfferInterfaceType {
                          __typename
                          id
                          title

                          ... on BuyType {
                            priceInCents
                            currency
                            buttonText
                          }

                          ... on RentType {
                            priceInCents
                            currency
                            buttonText
                            entitlementDurationSec
                          }

                          ... on SubscribeType {
                            priceInCents
                            currency
                            buttonText
                            trialPeriod {
                              length
                              unit
                            }
                            recurringPeriod {
                              length
                              unit
                            }
                          }

                          ... on PassType {
                            priceInCents
                            currency
                            buttonText
                          }
                        }

                        fragment PlayableFragment on Playable {
                          ...ChannelPlayableFragment
                          ...BroadcastPlayableFragment
                          ...VodPlayableFragment
                          ...LiveEventPlayableFragment
                        }

                        fragment ChannelPlayableFragment on ChannelPlayable {
                          id
                          kind
                          mms
                          mmsOrigCode
                          rights {
                            fastForward
                            pause
                            rewind
                          }
                        }

                        fragment BroadcastPlayableFragment on BroadcastPlayable {
                          id
                          kind
                          channel {
                            title
                            logoDark: image(type: \"logo-dark\")
                          }
                          startTimeUtc
                          duration
                          catchup {
                            from
                            to
                          }
                          watchOffset
                        }

                        fragment VodPlayableFragment on VodPlayable {
                          id
                          kind
                          duration
                          watchOffset
                        }

                        fragment LiveEventPlayableFragment on LiveEventPlayable {
                          id
                          kind
                          startTimeUtc
                        }
                        """
            })
        try:
            res = r.json()["data"]["viewer"]
        except json.JSONDecodeError:
            raise ValueError(f"Failed to load title data: {r.text}")
        return Title(
            id_=self.title,
            type_=Title.Types.MOVIE,
            name=res["viewable"]["title"],
            year=res["viewable"]["productionYear"],
            source=self.ALIASES[0],
            service_data=res,
        )

    def get_tracks(self, title):
        r = self.session.post(
            self.config["endpoints"]["entitlement"].format(id=title.service_data["viewable"]["defaultPlayable"]["id"])
        )
        try:
            self.entitlement_id = r.json()["token"]
        except json.JSONDecodeError:
            raise ValueError(f"Failed to load entitlement ID: {r.text}")

        r = self.session.post(
            self.config["endpoints"]["manifest"].format(id=title.service_data["viewable"]["defaultPlayable"]["id"]),
            headers={
                "Magine-Play-DeviceId": self.config["device"]["id"],
                "Magine-Play-DeviceModel": self.config["device"]["model"],
                "Magine-Play-DevicePlatform": self.config["device"]["platform"],
                "Magine-Play-DeviceType": self.config["device"]["type"],
                "Magine-Play-DRM": self.config["device"]["drm"],
                "Magine-Play-Protocol": self.config["device"]["protocol"],
                "Magine-Play-EntitlementId": self.entitlement_id
            }
        )
        try:
            manifest_data = r.json()
        except json.JSONDecodeError:
            raise ValueError(f"Failed to load entitlement ID: {r.text}")
        self.license_headers = manifest_data["headers"]

        tracks = Tracks.from_mpd(
            url=manifest_data["playlist"],
            session=self.session,
            source=self.ALIASES[0]
        )

        tracks.subtitles = [x for x in tracks.subtitles if x.codec == "vtt"]

        return tracks

    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        return None  # will use common privacy cert

    def license(self, challenge, **_):
        lic = self.session.post(
            self.config["endpoints"]["license"],
            headers=self.license_headers,
            data=challenge  # expects bytes
        )
        return lic.content  # bytes

    # Service specific functions

    def configure(self):
        self.log.info(" + Logging in")
        self.session.headers.update({
            "Magine-AccessToken": f"{self.config['device']['access_token']}"
        })
        self.login_data = self.login()
        self.session.headers.update({
            "authorization": f"Bearer {self.login_data['token']}"
        })

    def login(self):
        if not self.credentials:
            raise self.log.exit(" - No credentials provided, unable to log in.")
        r = self.session.post(
            self.config["endpoints"]["login"],
            json={
                "identity": self.credentials.username,
                "accessKey": self.credentials.password
            }
        )
        try:
            return r.json()
        except json.JSONDecodeError:
            raise ValueError(f"Failed to log in: {r.text}")
