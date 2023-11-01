import json
import os
import uuid
import requests
import time
import urllib.parse
from hashlib import md5
from uuid import UUID


import click

from vinetrimmer.objects import TextTrack, Title, Tracks, VideoTrack
from vinetrimmer.services.BaseService import BaseService
from vinetrimmer.utils import Cdm, try_get
from vinetrimmer.utils.collections import as_list
from vinetrimmer.vendor.pymp4.parser import Box
import m3u8
import re

class SonyLiv(BaseService):
    """
    Service code for Nine Digital's Stan. streaming service (https://stan.com.au).

    \b
    Authorization: Credentials 
    Security: UHD@L3, doesn't care about releases.
    """

    ALIASES = ["SONY", "sliv"]

    TITLE_RE = [
        r"^(?:https?://(?:www\.)?sonyliv\.com\/)?(?P<id>[a-z0-9-]+)",
    ]

    AUDIO_CODEC_MAP = {
        "AAC": "mp4a",
        "AC3": "ac-3",
        "EC3": "ec-3"
    }

    @staticmethod
    @click.command(name="SonyLiv", short_help="https://sonyliv.com")
    @click.argument("title", type=str, required=False)
    @click.pass_context
    def cli(ctx, **kwargs):
        return SonyLiv(ctx, **kwargs)

    def __init__(self, ctx, title):
        super().__init__(ctx)
        #self.parse_title(ctx, title)
        self.title = title

        assert ctx.parent is not None

        self.vcodec = ctx.parent.params["vcodec"]
        self.bearer = None
        self.dtinfo= None
        self.quality = ctx.parent.params["quality"]
        self.acodec = ctx.parent.params["acodec"] or "EC3"
        self.range = ctx.parent.params["range_"]
        

        self.profile = ctx.obj.profile

        self.device_id = None
        self.headers = {
                    'authorization': 'Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VySWQiOiIyMzA2MjExNTE1NTg5NjYzNzY2IiwidG9rZW4iOiJySUVLLXViWm8tM3lTbS04Q0NVLTNsNTIteUl3eS0zQiIsImV4cGlyYXRpb25Nb21lbnQiOiIyMDI0LTA2LTI2VDE1OjAwOjIwLjkxNFoiLCJpc1Byb2ZpbGVDb21wbGV0ZSI6ZmFsc2UsInNlc3Npb25DcmVhdGlvblRpbWUiOiIyMDIzLTA2LTI3VDE1OjAwOjIwLjkxNFoiLCJjaGFubmVsUGFydG5lcklEIjoiTVNNSU5EIiwiZmlyc3ROYW1lIjoiIiwibW9iaWxlTnVtYmVyIjoiOTQwMDgxOTQ5MSIsImRhdGVPZkJpcnRoIjoiIiwiZ2VuZGVyIjoiIiwicHJvZmlsZVBpYyI6IiIsInNvY2lhbFByb2ZpbGVQaWMiOiIiLCJzb2NpYWxMb2dpbklEIjpudWxsLCJzb2NpYWxMb2dpblR5cGUiOm51bGwsImlzRW1haWxWZXJpZmllZCI6dHJ1ZSwiaXNNb2JpbGVWZXJpZmllZCI6dHJ1ZSwibGFzdE5hbWUiOiIiLCJlbWFpbCI6IiIsImlzQ3VzdG9tZXJFbGlnaWJsZUZvckZyZWVUcmlhbCI6ZmFsc2UsImNvbnRhY3RJRCI6IjM4MjQ1NjMxOCIsImlhdCI6MTY4Nzg3ODAyMSwiZXhwIjoxNzE5NDE0MDIxfQ.GYWOhXekRk8BwmeU2X2CLr4vZa3TsxRU28te9OG__Ck',
                    'device_id': self.device_id,
                    'security_token': 'eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJpYXQiOjE2ODc4NzgwNTQsImV4cCI6MTY4OTE3NDA1NCwiYXVkIjoiKi5zb255bGl2LmNvbSIsImlzcyI6IlNvbnlMSVYiLCJzdWIiOiJzb21lQHNldGluZGlhLmNvbSJ9.N6wFjcIkZf637KWkSNJnXbkXhFPuhG5YNuZToqZYb-W-MG8W6m5eHE0keX-lRXJQemXcSTRSCxO36trK2iJg-wModWoU33krD9hqf5AzXyBMTblDjkavpq8udFcpDNgfWfQgbqx3gt0-7NknuZKNi38KKb4LJMf470a9Rc81btgJydIHHM6bL8LcYuQjiFIM0Gyv-uq1evhYWhZJQV6psAOlbvXY8hf10smhCyHT9c9z4qOAZ4S4XGZnqv5xfh-T9rIp4iA_AUnNqMe9WxoFJWZL0mOvYc8Xm-6jAouSXwdlWETmsD2DXnvkHUqRH85x9Prq5XN4EGsK-NSQblUUsWjWz5qQV-SHKMxO0EMw8WiEMMFTSXgRl9lecrrVBSWzlWQ5vw4BV7X3bYPR_bC-SJFNxQQcuIR2LCQbJgax1B-lBMMCeoZ50TNj5DbDakPAQbqTWq_y-D6jt73jEmxRsZzHDMlTIYt9Q3fSrnzTG_vQDkjnKQ8oMLXK-NUAvVfyn4SmfymX4PeMFbOQPd34KThz6nwb1iS-Sv1LaKqMZcWpzR4bwstlpPP0pxeR9wkgSjMl8lfNXknxJzhkZiWNf5qetkl5b2pR4wLeVCIMHXze8E5PsNxcjiH2ZTqfAQ4GcWGJMzmfDE4c6Qrxkj9zl1aA8WcIAGNL91XyhrOuErQ',
                    'x-via-device': 'true',
                }
        self.token = None
        self.license_api = None
        print(self.title)

        self.configure()

    def get_titles(self):
        r = self.session.post(
            url=f'https://apiv2.sonyliv.com/AGL/2.7/A/ENG/FIRE_TV/IN/{self.state_code}/DETAIL/{self.title}?kids_safe=false',
            headers={
                "Content-Type": "application/json",
                "x-via-device": "true",
                "Host": "apiv2.sonyliv.com",
                "session-id": self.session_id,
                "user-agent": "com.onemainstream.sonyliv.android/8.95 (Android 7.1.2; en_IN; AFTMM; Build/NS6281 )",
                "build_number": "10491",
                "app_version": "6.12.35",
                "security_token": self.security_token,
                "device_id": self.device_id,
            }
        )
        try:
            res = r.json()['resultObj']['containers'][0]
        except json.JSONDecodeError:
            raise ValueError(f"Failed to load title manifest: {res.text}")

        if res['metadata']['contentSubtype'] == 'MOVIE':
            return Title(
                id_=self.title,
                type_=Title.Types.MOVIE,
                name=res['metadata']["title"],
                year=res['metadata']["year"],
                original_lang=res['metadata']["language"],
                source=self.ALIASES[0],
                service_data=res,
            )
        else:
            season_data = []
            info_dict = {}
            for x in res['containers']:
                season_id = x['id']
                r = self.session.post(
                    url=f'https://apiv2.sonyliv.com/AGL/2.7/A/ENG/FIRE_TV/IN/{self.state_code}/DETAIL/{season_id}?kids_safe=false',
                    headers={
                        "Content-Type": "application/json",
                        "x-via-device": "true",
                        "Host": "apiv2.sonyliv.com",
                        "session-id": self.session_id,
                        "user-agent": "com.onemainstream.sonyliv.android/8.95 (Android 7.1.2; en_IN; AFTMM; Build/NS6281 )",
                        "build_number": "10491",
                        "app_version": "6.12.35",
                        "security_token": self.security_token,
                        "device_id": self.device_id,
                    }
                ).json()
                for y in r['resultObj']['containers'][0]['containers']:
                    info_dict = {'id': self.title,
                                 'name': res['metadata']["title"],
                                 'year': y['metadata']['year'],
                                 'season': x['metadata']['season'],
                                 'episode': y['metadata']['episodeNumber'],
                                 'episodename': y['metadata']['episodeTitle'],
                                 'originallang': y['metadata']['language'],
                                 'servicedata': y
                                 }
                    season_data.append(info_dict)

            return [Title(
                id_=self.title,
                type_=Title.Types.TV,
                name=res["metadata"]["title"],
                year=x["year"],
                season=x["season"],
                episode=x["episode"],
                episode_name=x["episodename"],
                original_lang=x["originallang"],
                source=self.ALIASES[0],
                service_data=x['servicedata']
            ) for x in season_data]

    def get_tracks(self, title):
        if self.vcodec == 'H265':
            if self.range == 'DV':
                client = '{"device_make":"Amazon","device_model":"AFTMM","display_res":"2160","viewport_res":"2160","supp_codec":"HEVC,H264,AAC,EAC3,AC3,ATMOS","audio_decoder":"EAC3,AAC,AC3,ATMOS","hdr_decoder":"DOLBY_VISION","td_user_useragent":"com.onemainstream.sonyliv.android\/8.95 (Android 7.1.2; en_IN; AFTMM; Build\/NS6281 )"}'
            elif self.range == 'HDR10':
                client = '{"device_make":"Amazon","device_model":"AFTMM","display_res":"2160","viewport_res":"2160","supp_codec":"HEVC,H264,AAC,EAC3,AC3,ATMOS","audio_decoder":"EAC3,AAC,AC3,ATMOS","hdr_decoder":"HDR10","td_user_useragent":"com.onemainstream.sonyliv.android\/8.95 (Android 7.1.2; en_IN; AFTMM; Build\/NS6281 )"}'
            elif self.range == 'SDR':
                client = '{"device_make":"Amazon","device_model":"AFTMM","display_res":"2160","viewport_res":"2160","supp_codec":"HEVC,H264,AAC,EAC3,AC3,ATMOS","audio_decoder":"EAC3,AAC,AC3,ATMOS","hdr_decoder":"HLG","td_user_useragent":"com.onemainstream.sonyliv.android\/8.95 (Android 7.1.2; en_IN; AFTMM; Build\/NS6281 )"}'

        if self.vcodec == 'H265':
            r = self.session.post(
                url=f"https://apiv2.sonyliv.com/AGL/3.3/SR/ENG/SONY_ANDROID_TV/IN/{self.state_code}/CONTENT/VIDEOURL/VOD/{title.service_data['id']}?kids_safe=false&contactId={self.contact_id}",
                headers={
                    "Content-Type": "application/json",
                    "x-via-device": "true",
                    "Host": "apiv2.sonyliv.com",
                    "Authorization": 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VySWQiOiIyMzA2MjExNTE1NTg5NjYzNzY2IiwidG9rZW4iOiJySUVLLXViWm8tM3lTbS04Q0NVLTNsNTIteUl3eS0zQiIsImV4cGlyYXRpb25Nb21lbnQiOiIyMDI0LTA2LTI2VDE1OjAwOjIwLjkxNFoiLCJpc1Byb2ZpbGVDb21wbGV0ZSI6ZmFsc2UsInNlc3Npb25DcmVhdGlvblRpbWUiOiIyMDIzLTA2LTI3VDE1OjAwOjIwLjkxNFoiLCJjaGFubmVsUGFydG5lcklEIjoiTVNNSU5EIiwiZmlyc3ROYW1lIjoiIiwibW9iaWxlTnVtYmVyIjoiOTQwMDgxOTQ5MSIsImRhdGVPZkJpcnRoIjoiIiwiZ2VuZGVyIjoiIiwicHJvZmlsZVBpYyI6IiIsInNvY2lhbFByb2ZpbGVQaWMiOiIiLCJzb2NpYWxMb2dpbklEIjpudWxsLCJzb2NpYWxMb2dpblR5cGUiOm51bGwsImlzRW1haWxWZXJpZmllZCI6dHJ1ZSwiaXNNb2JpbGVWZXJpZmllZCI6dHJ1ZSwibGFzdE5hbWUiOiIiLCJlbWFpbCI6IiIsImlzQ3VzdG9tZXJFbGlnaWJsZUZvckZyZWVUcmlhbCI6ZmFsc2UsImNvbnRhY3RJRCI6IjM4MjQ1NjMxOCIsImlhdCI6MTY4Nzg3ODAyMSwiZXhwIjoxNzE5NDE0MDIxfQ.GYWOhXekRk8BwmeU2X2CLr4vZa3TsxRU28te9OG__Ck',
                    "session-id": self.session_id,
                    "user-agent": "com.onemainstream.sonyliv.android/8.95 (Android 7.1.2; en_IN; AFTMM; Build/NS6281 )",
                    "build_number": "10491",
                    "app_version": "6.12.35",
                    "security_token": self.security_token,
                    "device_id": self.device_id,
                    'Td_client_hints': client
                }
            )
            self.session.headers.update({"x-playback-session-id": f'{uuid.uuid4().hex}-{time.time() * 1000}'})
        else:
            r = self.session.post(
                url=f"https://apiv2.sonyliv.com/AGL/3.3/SR/ENG/SONY_ANDROID_TV/IN/{self.state_code}/CONTENT/VIDEOURL/VOD/{title.service_data['id']}?kids_safe=false&contactId={self.contact_id}",
                headers={
                    "Content-Type": "application/json",
                    "x-via-device": "true",
                    "Host": "apiv2.sonyliv.com",
                    "Authorization": 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VySWQiOiIyMzA2MjExNTE1NTg5NjYzNzY2IiwidG9rZW4iOiJySUVLLXViWm8tM3lTbS04Q0NVLTNsNTIteUl3eS0zQiIsImV4cGlyYXRpb25Nb21lbnQiOiIyMDI0LTA2LTI2VDE1OjAwOjIwLjkxNFoiLCJpc1Byb2ZpbGVDb21wbGV0ZSI6ZmFsc2UsInNlc3Npb25DcmVhdGlvblRpbWUiOiIyMDIzLTA2LTI3VDE1OjAwOjIwLjkxNFoiLCJjaGFubmVsUGFydG5lcklEIjoiTVNNSU5EIiwiZmlyc3ROYW1lIjoiIiwibW9iaWxlTnVtYmVyIjoiOTQwMDgxOTQ5MSIsImRhdGVPZkJpcnRoIjoiIiwiZ2VuZGVyIjoiIiwicHJvZmlsZVBpYyI6IiIsInNvY2lhbFByb2ZpbGVQaWMiOiIiLCJzb2NpYWxMb2dpbklEIjpudWxsLCJzb2NpYWxMb2dpblR5cGUiOm51bGwsImlzRW1haWxWZXJpZmllZCI6dHJ1ZSwiaXNNb2JpbGVWZXJpZmllZCI6dHJ1ZSwibGFzdE5hbWUiOiIiLCJlbWFpbCI6IiIsImlzQ3VzdG9tZXJFbGlnaWJsZUZvckZyZWVUcmlhbCI6ZmFsc2UsImNvbnRhY3RJRCI6IjM4MjQ1NjMxOCIsImlhdCI6MTY4Nzg3ODAyMSwiZXhwIjoxNzE5NDE0MDIxfQ.GYWOhXekRk8BwmeU2X2CLr4vZa3TsxRU28te9OG__Ck',
                    "session-id": self.session_id,
                    "user-agent": "com.onemainstream.sonyliv.android/8.95 (Android 7.1.2; en_IN; AFTMM; Build/NS6281 )",
                    "build_number": "10491",
                    "app_version": "6.12.35",
                    "security_token": self.security_token,
                    "device_id": self.device_id,
                    'Td_client_hints': '{"os_name":"Windows","os_version":"10","device_make":"none","device_model":"none","display_res":"1536","viewport_res":"811","conn_type":"WIFI","supp_codec":"H264,,AV1,AAC","audio_decoder":"STEREO","hdr_decoder": "UNKNOWN" ,"client_throughput":16000,"td_user_agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.0.0 Safari/537.36"}'
                }
            )
        try:
            res = r.json()
        except json.JSONDecodeError:
            raise ValueError(f"Failed to load title manifest: {res.text}")

        mpd_url = res["resultObj"]["videoURL"]

        res1 = self.session.post(
            url=f'https://apiv2.sonyliv.com/AGL/2.4/SR/ENG/FIRE_TV/IN/{self.state_code}/CONTENT/GETLAURL',
            headers={
                "Content-Type": "application/json",
                "x-via-device": "true",
                "Host": "apiv2.sonyliv.com",
                "Authorization": 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VySWQiOiIyMzA2MjExNTE1NTg5NjYzNzY2IiwidG9rZW4iOiJySUVLLXViWm8tM3lTbS04Q0NVLTNsNTIteUl3eS0zQiIsImV4cGlyYXRpb25Nb21lbnQiOiIyMDI0LTA2LTI2VDE1OjAwOjIwLjkxNFoiLCJpc1Byb2ZpbGVDb21wbGV0ZSI6ZmFsc2UsInNlc3Npb25DcmVhdGlvblRpbWUiOiIyMDIzLTA2LTI3VDE1OjAwOjIwLjkxNFoiLCJjaGFubmVsUGFydG5lcklEIjoiTVNNSU5EIiwiZmlyc3ROYW1lIjoiIiwibW9iaWxlTnVtYmVyIjoiOTQwMDgxOTQ5MSIsImRhdGVPZkJpcnRoIjoiIiwiZ2VuZGVyIjoiIiwicHJvZmlsZVBpYyI6IiIsInNvY2lhbFByb2ZpbGVQaWMiOiIiLCJzb2NpYWxMb2dpbklEIjpudWxsLCJzb2NpYWxMb2dpblR5cGUiOm51bGwsImlzRW1haWxWZXJpZmllZCI6dHJ1ZSwiaXNNb2JpbGVWZXJpZmllZCI6dHJ1ZSwibGFzdE5hbWUiOiIiLCJlbWFpbCI6IiIsImlzQ3VzdG9tZXJFbGlnaWJsZUZvckZyZWVUcmlhbCI6ZmFsc2UsImNvbnRhY3RJRCI6IjM4MjQ1NjMxOCIsImlhdCI6MTY4Nzg3ODAyMSwiZXhwIjoxNzE5NDE0MDIxfQ.GYWOhXekRk8BwmeU2X2CLr4vZa3TsxRU28te9OG__Ck',
                "session-id": self.session_id,
                "user-agent": "com.onemainstream.sonyliv.android/8.95 (Android 7.1.2; en_IN; AFTMM; Build/NS6281 )",
                "build_number": "10491",
                "app_version": "6.12.35",
                "security_token": self.security_token,
                "device_id": self.device_id,
            },
            json={
                "actionType": "play",
                "assetId": title.service_data['id'],
                "browser": "chrome",
                "deviceId": self.device_id,
                "os": "android",
                "platform": "web"
            }
        )
        self.license_api = res1.json()['resultObj']['laURL']

        self.session.headers.update({
            'Host': "drm.sonyliv.com",
            "User-Agent": "com.onemainstream.sonyliv.android/8.95 (Android 7.1.2; en_IN; AFTMM; Build/NS6281 )",
            "Authorization": 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VySWQiOiIyMzA2MjExNTE1NTg5NjYzNzY2IiwidG9rZW4iOiJySUVLLXViWm8tM3lTbS04Q0NVLTNsNTIteUl3eS0zQiIsImV4cGlyYXRpb25Nb21lbnQiOiIyMDI0LTA2LTI2VDE1OjAwOjIwLjkxNFoiLCJpc1Byb2ZpbGVDb21wbGV0ZSI6ZmFsc2UsInNlc3Npb25DcmVhdGlvblRpbWUiOiIyMDIzLTA2LTI3VDE1OjAwOjIwLjkxNFoiLCJjaGFubmVsUGFydG5lcklEIjoiTVNNSU5EIiwiZmlyc3ROYW1lIjoiIiwibW9iaWxlTnVtYmVyIjoiOTQwMDgxOTQ5MSIsImRhdGVPZkJpcnRoIjoiIiwiZ2VuZGVyIjoiIiwicHJvZmlsZVBpYyI6IiIsInNvY2lhbFByb2ZpbGVQaWMiOiIiLCJzb2NpYWxMb2dpbklEIjpudWxsLCJzb2NpYWxMb2dpblR5cGUiOm51bGwsImlzRW1haWxWZXJpZmllZCI6dHJ1ZSwiaXNNb2JpbGVWZXJpZmllZCI6dHJ1ZSwibGFzdE5hbWUiOiIiLCJlbWFpbCI6IiIsImlzQ3VzdG9tZXJFbGlnaWJsZUZvckZyZWVUcmlhbCI6ZmFsc2UsImNvbnRhY3RJRCI6IjM4MjQ1NjMxOCIsImlhdCI6MTY4Nzg3ODAyMSwiZXhwIjoxNzE5NDE0MDIxfQ.GYWOhXekRk8BwmeU2X2CLr4vZa3TsxRU28te9OG__Ck',
            "x-playback-session-id":  f'{uuid.uuid4().hex}-{time.time() * 1000}',
        })

        tracks = Tracks.from_mpd(
            url=mpd_url,
            session=self.session,
            source=self.ALIASES[0]
        )

        if self.vcodec == 'H264':
            r = self.session.post(
                url=f"https://apiv2.sonyliv.com/AGL/3.3/SR/ENG/SONY_ANDROID_TV/IN/{self.state_code}/CONTENT/VIDEOURL/VOD/{title.service_data['id']}?kids_safe=false&contactId={self.contact_id}",
                headers={
                    "Content-Type": "application/json",
                    "x-via-device": "true",
                    "Host": "apiv2.sonyliv.com",
                    "Authorization": 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VySWQiOiIyMzA2MjExNTE1NTg5NjYzNzY2IiwidG9rZW4iOiJySUVLLXViWm8tM3lTbS04Q0NVLTNsNTIteUl3eS0zQiIsImV4cGlyYXRpb25Nb21lbnQiOiIyMDI0LTA2LTI2VDE1OjAwOjIwLjkxNFoiLCJpc1Byb2ZpbGVDb21wbGV0ZSI6ZmFsc2UsInNlc3Npb25DcmVhdGlvblRpbWUiOiIyMDIzLTA2LTI3VDE1OjAwOjIwLjkxNFoiLCJjaGFubmVsUGFydG5lcklEIjoiTVNNSU5EIiwiZmlyc3ROYW1lIjoiIiwibW9iaWxlTnVtYmVyIjoiOTQwMDgxOTQ5MSIsImRhdGVPZkJpcnRoIjoiIiwiZ2VuZGVyIjoiIiwicHJvZmlsZVBpYyI6IiIsInNvY2lhbFByb2ZpbGVQaWMiOiIiLCJzb2NpYWxMb2dpbklEIjpudWxsLCJzb2NpYWxMb2dpblR5cGUiOm51bGwsImlzRW1haWxWZXJpZmllZCI6dHJ1ZSwiaXNNb2JpbGVWZXJpZmllZCI6dHJ1ZSwibGFzdE5hbWUiOiIiLCJlbWFpbCI6IiIsImlzQ3VzdG9tZXJFbGlnaWJsZUZvckZyZWVUcmlhbCI6ZmFsc2UsImNvbnRhY3RJRCI6IjM4MjQ1NjMxOCIsImlhdCI6MTY4Nzg3ODAyMSwiZXhwIjoxNzE5NDE0MDIxfQ.GYWOhXekRk8BwmeU2X2CLr4vZa3TsxRU28te9OG__Ck',
                    "session-id": self.session_id,
                    "user-agent": "com.onemainstream.sonyliv.android/8.95 (Android 7.1.2; en_IN; AFTMM; Build/NS6281 )",
                    "build_number": "10491",
                    "app_version": "6.12.35",
                    "security_token": 'eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJpYXQiOjE2ODc4NzgwNTQsImV4cCI6MTY4OTE3NDA1NCwiYXVkIjoiKi5zb255bGl2LmNvbSIsImlzcyI6IlNvbnlMSVYiLCJzdWIiOiJzb21lQHNldGluZGlhLmNvbSJ9.N6wFjcIkZf637KWkSNJnXbkXhFPuhG5YNuZToqZYb-W-MG8W6m5eHE0keX-lRXJQemXcSTRSCxO36trK2iJg-wModWoU33krD9hqf5AzXyBMTblDjkavpq8udFcpDNgfWfQgbqx3gt0-7NknuZKNi38KKb4LJMf470a9Rc81btgJydIHHM6bL8LcYuQjiFIM0Gyv-uq1evhYWhZJQV6psAOlbvXY8hf10smhCyHT9c9z4qOAZ4S4XGZnqv5xfh-T9rIp4iA_AUnNqMe9WxoFJWZL0mOvYc8Xm-6jAouSXwdlWETmsD2DXnvkHUqRH85x9Prq5XN4EGsK-NSQblUUsWjWz5qQV-SHKMxO0EMw8WiEMMFTSXgRl9lecrrVBSWzlWQ5vw4BV7X3bYPR_bC-SJFNxQQcuIR2LCQbJgax1B-lBMMCeoZ50TNj5DbDakPAQbqTWq_y-D6jt73jEmxRsZzHDMlTIYt9Q3fSrnzTG_vQDkjnKQ8oMLXK-NUAvVfyn4SmfymX4PeMFbOQPd34KThz6nwb1iS-Sv1LaKqMZcWpzR4bwstlpPP0pxeR9wkgSjMl8lfNXknxJzhkZiWNf5qetkl5b2pR4wLeVCIMHXze8E5PsNxcjiH2ZTqfAQ4GcWGJMzmfDE4c6Qrxkj9zl1aA8WcIAGNL91XyhrOuErQ',
                    "device_id": self.device_id,
                    'Td_client_hints': '{"device_make":"Amazon","device_model":"AFTMM","display_res":"2160","viewport_res":"2160","supp_codec":"HEVC,H264,AAC,EAC3,AC3,ATMOS","audio_decoder":"EAC3,AAC,AC3,ATMOS","hdr_decoder":"HLG","td_user_useragent":"com.onemainstream.sonyliv.android\/8.95 (Android 7.1.2; en_IN; AFTMM; Build\/NS6281 )"}',
                    }
            ).json()

            audio_mpd_url = r["resultObj"]["videoURL"]

            self.session.headers.update({
            'Host': "drm.sonyliv.com",
            "User-Agent": "com.onemainstream.sonyliv.android/8.95 (Android 7.1.2; en_IN; AFTMM; Build/NS6281 )",
            "Authorization": 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VySWQiOiIyMzA2MjExNTE1NTg5NjYzNzY2IiwidG9rZW4iOiJySUVLLXViWm8tM3lTbS04Q0NVLTNsNTIteUl3eS0zQiIsImV4cGlyYXRpb25Nb21lbnQiOiIyMDI0LTA2LTI2VDE1OjAwOjIwLjkxNFoiLCJpc1Byb2ZpbGVDb21wbGV0ZSI6ZmFsc2UsInNlc3Npb25DcmVhdGlvblRpbWUiOiIyMDIzLTA2LTI3VDE1OjAwOjIwLjkxNFoiLCJjaGFubmVsUGFydG5lcklEIjoiTVNNSU5EIiwiZmlyc3ROYW1lIjoiIiwibW9iaWxlTnVtYmVyIjoiOTQwMDgxOTQ5MSIsImRhdGVPZkJpcnRoIjoiIiwiZ2VuZGVyIjoiIiwicHJvZmlsZVBpYyI6IiIsInNvY2lhbFByb2ZpbGVQaWMiOiIiLCJzb2NpYWxMb2dpbklEIjpudWxsLCJzb2NpYWxMb2dpblR5cGUiOm51bGwsImlzRW1haWxWZXJpZmllZCI6dHJ1ZSwiaXNNb2JpbGVWZXJpZmllZCI6dHJ1ZSwibGFzdE5hbWUiOiIiLCJlbWFpbCI6IiIsImlzQ3VzdG9tZXJFbGlnaWJsZUZvckZyZWVUcmlhbCI6ZmFsc2UsImNvbnRhY3RJRCI6IjM4MjQ1NjMxOCIsImlhdCI6MTY4Nzg3ODAyMSwiZXhwIjoxNzE5NDE0MDIxfQ.GYWOhXekRk8BwmeU2X2CLr4vZa3TsxRU28te9OG__Ck',
            "x-playback-session-id":  f'{uuid.uuid4().hex}-{time.time() * 1000}',
        })


            audio_tracks = Tracks.from_mpd(
                url = audio_mpd_url,
                session = self.session,
                source = self.ALIASES[0]
            )
            tracks.audios = audio_tracks.audios
            sub_url = title.service_data["platformVariants"][0]["subtitlesLanguages"][0].get("subtitleUrl")
            sub_language = title.service_data["platformVariants"][0]["subtitlesLanguages"][0].get("subtitleLanguageName")
            if sub_url:
                tracks.add(
                    TextTrack(
                        id=md5(sub_url.encode()).hexdigest()[0:6],
                        source=self.ALIASES[0],
                        url=sub_url,
                        codec='vtt',
                        language=sub_language,
                        forced=False,
                        sdh=False
                    )
                )
        for track in tracks:
            track.needs_proxy = True
        return tracks

    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        return None  # will use common privacy cert

    def license(self, challenge, **_):
        return requests.post(
            url=self.license_api,
            data=challenge,  # expects bytes
            headers={
                "Content-Type": "application/octet-stream",
                "Host": "wv.service.expressplay.com",
                "User-Agent": "com.onemainstream.sonyliv.android/8.95 (Android 7.1.2; en_IN; AFTMM; Build/NS6281 )",
                'x-playback-session-id': f'{uuid.uuid4().hex}-{time.time() * 1000}'
            }
        ).content

    # Service specific functions

    def configure(self):
        self.log.info("Logging into SonyLiv")
        self.device_id = self.get_device_id()
        self.log.info(f" + Using Device ID: {self.device_id}")
        self.session_id = f'{str(uuid.uuid4().hex)}'
        self.security_token = self.get_security_token()
        self.state_code, self.city, self.channelpartnerid, = self.get_ULD()
        self.token = self.get_token()
        self.contact_id = self.get_contact_id()

    def get_security_token(self):
        data = self.session.get(
            url='https://apiv2.sonyliv.com/AGL/1.5/A/ENG/FIRE_TV/IN/GETTOKEN',
            headers={
                "Host": "apiv2.sonyliv.com",
                "user-agent": "okhttp/3.14.9"
            }
        ).json()

        return data["resultObj"]

    def get_ULD(self):
        data = self.session.get(
            url="https://apiv2.sonyliv.com/AGL/1.5/A/ENG/FIRE_TV/IN/USER/ULD",
            headers={
                "Content-Type": "application/json",
                "x-via-device": "true",
                "Host": "apiv2.sonyliv.com",
                "session-id": self.session_id,
                "user-agent": "com.onemainstream.sonyliv.android/8.95 (Android 7.1.2; en_IN; AFTMM; Build/NS6281 )",
                "build_number": "10491",
                "app_version": "6.12.35",
                "security_token": self.security_token,
                "device_id": self.device_id,
            }
        ).json()

        return data["resultObj"].get("state_code"), data["resultObj"].get("city"), data["resultObj"].get(
            "channelPartnerID")

    def get_contact_id(self):
        data = self.session.get(
            url=f"https://apiv2.sonyliv.com/AGL/3.3/A/ENG/FIRE_TV/IN/{self.state_code}/GETPROFILE?channelPartnerID={self.channelpartnerid}",
            headers={
                "Content-Type": "application/json",
                "x-via-device": "true",
                "Host": "apiv2.sonyliv.com",
                "session-id": self.session_id,
                "user-agent": "com.onemainstream.sonyliv.android/8.95 (Android 7.1.2; en_IN; AFTMM; Build/NS6281 )",
                "build_number": "10491",
                "app_version": "6.12.35",
                "Authorization": self.token,
                "security_token": self.security_token,
                "device_id": self.device_id,
            }
        ).json()
        return data['resultObj']['contactMessage'][0].get('contactID')

    def get_device_id(self):
        to = self.get_cache("deviceid_{profile}.json".format(profile=self.profile))
        if os.path.isfile(to):
            with open(to, encoding="utf-8") as fd:
                device_id = json.load(fd)
                return device_id['deviceid']
        unique_id = uuid.uuid4().hex[:16]
        data = {"deviceid": unique_id}
        os.makedirs(os.path.dirname(to), exist_ok=True)
        with open(to, "w", encoding="utf-8") as fd:
            json.dump(data, fd)
        return unique_id

    def get_token(self):
        token_cache_path = self.get_cache("token_{profile}.json".format(profile=self.profile))
        if os.path.isfile(token_cache_path):
            with open(token_cache_path, encoding="utf-8") as fd:
                token = json.load(fd)
            if True:
                # not expired, lets use
                self.log.info(" + Using cached auth tokens...")
                return token["access_token"]
        # get new token
        token = self.login()
        return self.save_token(token, token_cache_path)

    @staticmethod
    def save_token(token, to):
        os.makedirs(os.path.dirname(to), exist_ok=True)
        with open(to, "w", encoding="utf-8") as fd:
            json.dump({'access_token': token}, fd)
        return token

    def login(self):
            res = self.session.post(
                url=f'https://apiv2.sonyliv.com/AGL/1.5/A/ENG/FIRE_TV/IN/GENERATEDEVICEACTIVATIONCODE',
                headers={
                    "Content-Type": "application/json",
                    "x-via-device": "true",
                    "Host": "apiv2.sonyliv.com",
                    "session-id": self.session_id,
                    "user-agent": "com.onemainstream.sonyliv.android/8.95 (Android 7.1.2; en_IN; AFTMM; Build/NS6281 )",
                    "build_number": "10491",
                    "app_version": "6.12.35",
                    "security_token": self.security_token,
                    "device_id": self.device_id,
                },
                json={
                    "channelPartnerID": self.channelpartnerid,
                    'deviceBrand': 'Amazon',
                    'deviceModelNumber': 'AmazonAFTMM',
                    'deviceName': 'Fire TV Sony Liv',
                    'deviceType': 'FireTV',
                    'location': self.city,
                    'serialNo': self.device_id
                }
            ).json()
            code = res['resultObj']['activationCode']
            self.log.info(f"Go to https://www.sonyliv.com/device/activate and enter {code}")
            devicecode_choice = input("Did you enter the code as informed above? (y/n): ")
            if devicecode_choice.lower() == "y" or devicecode_choice.lower() == "yes":
                r = self.session.post(
                    url=f'https://apiv2.sonyliv.com/AGL/1.5/A/ENG/FIRE_TV/IN/GENERATEDEVICEACTIVATIONCODE',
                    headers={
                        "Content-Type": "application/json",
                        "x-via-device": "true",
                        "Host": "apiv2.sonyliv.com",
                        "session-id": self.session_id,
                        "user-agent": "com.onemainstream.sonyliv.android/8.95 (Android 7.1.2; en_IN; AFTMM; Build/NS6281 )",
                        "build_number": "10491",
                        "app_version": "6.12.35",
                        "security_token": self.security_token,
                        "device_id": self.device_id,
                    },
                    json={
                        "channelPartnerID": self.channelpartnerid,
                        'deviceBrand': 'Amazon',
                        'deviceModelNumber': 'AmazonAFTMM',
                        'deviceName': 'Fire TV Sony Liv',
                        'deviceType': 'FireTV',
                        'location': self.city,
                        'serialNo': self.device_id
                    }
                ).json()
                return r['resultObj']['accessToken']
            else:
                self.log.exit("Try again.")