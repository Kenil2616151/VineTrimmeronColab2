#!/usr/bin/env python3

import base64
import json
import logging
import os

import click
import requests
import yaml
from Cryptodome.Cipher import AES
from Cryptodome.Hash import CMAC, HMAC, SHA256
from Cryptodome.Random import get_random_bytes
from Cryptodome.Util import Padding

from vinetrimmer.utils.widevine.device import LocalDevice
from vinetrimmer.utils.widevine.keybox import Keybox
from vinetrimmer.utils.widevine.protos.widevine_pb2 import (ClientIdentificationRaw, ProtocolVersion,
                                                            ProvisioningOptions, ProvisioningRequest,
                                                            ProvisioningResponse, SignedProvisioningMessage)


def generate_derived_keys(msg, key):
    """
    Returns 3 keys.

    For provisioning:
    - enc: aes key used for unwrapping RSA key out of response
    - auth_1: hmac-sha256 key used for verifying provisioning response
    - auth_2: hmac-sha256 key used for signing provisioning request
    When used with a session key instead of a device key:
    - enc: decrypting content and other keys
    - auth_1: verifying response
    - auth_2: renewals

    with key as pre-provision device key, it can be used to provision and get a RSA device key and token/cert
    with key as session key (OAEP wrapped with the post-provision RSA device key), it can be used to decrypt content
    and signing keys and verify licenses
    """
    enc_key_base = b"ENCRYPTION\000" + msg + b"\0\0\0\x80"
    auth_key_base = b"AUTHENTICATION\0" + msg + b"\0\0\2\0"

    cmac_obj = CMAC.new(key, ciphermod=AES)
    cmac_obj.update(b"\x01" + enc_key_base)

    enc_cmac_key = cmac_obj.digest()

    cmac_obj = CMAC.new(key, ciphermod=AES)
    cmac_obj.update(b"\x01" + auth_key_base)
    auth_cmac_key_1 = cmac_obj.digest()

    cmac_obj = CMAC.new(key, ciphermod=AES)
    cmac_obj.update(b"\x02" + auth_key_base)
    auth_cmac_key_2 = cmac_obj.digest()

    cmac_obj = CMAC.new(key, ciphermod=AES)
    cmac_obj.update(b"\x03" + auth_key_base)
    auth_cmac_key_3 = cmac_obj.digest()

    cmac_obj = CMAC.new(key, ciphermod=AES)
    cmac_obj.update(b"\x04" + auth_key_base)
    auth_cmac_key_4 = cmac_obj.digest()

    return enc_cmac_key, auth_cmac_key_1 + auth_cmac_key_2, auth_cmac_key_3 + auth_cmac_key_4


@click.command(name="prv")
@click.argument("keybox")
@click.option("-p", "--proxy", type=str, default=None, help="Proxy to tunnel requests through.")
@click.option("-u", "--user-agent", type=str, default=None, help="User-Agent to supply with requests.")
def prv(keybox_path, proxy=None, user_agent=None):
    """
    Provision a Keybox and receive a Widevine-ready WVD file.

    The WVD file will be placed next to the input keybox with the .wvd file extension with security
    level and system ID information appended to the filename.

    There must be a config file next to the keybox, with the same filename but .yml extension.
    This config file contains the unique device configuration values that cannot be retrieved from
    the keybox. I recommend that the config yaml file is kept for archival and look-back purposes.

    Example config:
    Warning: these values may not be correct or values used in up-to-date devices, and are definitely not
    correct for your specific device. If you make a Widevine license request to a demo player and disable
    service/privacy certificates (block the request maybe), then you will see the real original Client ID
    and the data it uses for client_info and capabilities. In fact, you could just use that (but swap out
    the token to the new provision token).

        [wvd]
        security_level = 1
        device_type = 'android'
        send_key_control_nonce = true

        [client_info]
        company_name = 'motorola'
        model_name = 'Nexus 6'
        architecture_name = 'armeabi-v7a'
        device_name = 'shamu'
        product_name = 'shamu'
        build_info = 'google/shamu/shamu:5.1.1/LMY48M/2167285:user/release-keys'
        os_version = '5.1.12'

        [capabilities]
        session_token = 1
        max_hdcp_version = 'HDCP_V2_2'
        oem_crypto_api_version = 11

    You can get some of the client_info from the build.props file from the devices system image or an
    update file. Some data can also be retrieved from "DRM Info" apps (there's plenty of them).

    Example corresponding props, name and the prop in the .prop file, in correct order:

        "company_name"      "ro.product.manufacturer"
        "model_name"        "ro.product.model"
        "architecture_name" "ro.product.cpu.abi"
        "device_name"       "ro.product.device"
        "product_name"      "ro.product.name"
        "build_info"        "ro.build.fingerprint"

    "device_id" from keybox/oemcrypto/tz - in verbose (default) mode, this will appear as "stable id" in
    the log (part of keybox[0:0x20]).

    "widevine_cdm_version", this is hardcoded in libwvdrm.so (usermode) a string like "v5.0.0-android" or
    "v4.1.0-android", either disassemble or try finding a string close to this in libwidevinecdm.so,
    libwvdrmengine.so or libwvhidl.so, depending on which library is used to handle widevine outside of trustzone.

    "oem_crypto_security_patch_level" is usually 0 and requires calling or disassembling liboemcrypto to get.

    HDCP 2.2 is often supported on most non-desktop/level 1 capable devices.
    """
    log = logging.getLogger("prv")

    if not proxy:
        log.warning("No proxy provided...")

    if not os.path.isfile(keybox_path):
        raise log.exit(" - Keybox path provided does not exist, or is not a file.")
    config_path = f"{keybox_path}.yml"
    if not os.path.isfile(config_path):
        raise log.exit(f" - Config path does not exist, or is not a file. Make sure it exists at: {config_path}")

    with open(config_path) as fd:
        config = yaml.safe_load(fd)

    if not config:
        raise log.exit(" - Config is empty, that's surely a mistake, right?")

    log.info(f"Config data:\n{json.dumps(config, sort_keys=True, indent=4)}")

    keybox = Keybox.load(keybox_path)

    log.info(f"Keybox loaded: {repr(keybox)}")
    log.info(f"Likely a {'consumer' if keybox.flags & 2 == 2 else 'test'} device keybox")

    client_id = ClientIdentificationRaw()
    client_id.Type = ClientIdentificationRaw.KEYBOX
    client_id.Token = keybox.device_id

    # defaults, but they appear to be set by real clients if you check the bit stream
    provisioning_options = ProvisioningOptions()
    provisioning_options.certificate_type = ProvisioningOptions.WIDEVINE_DRM
    provisioning_options.certificate_authority = ""

    provisioning_request = ProvisioningRequest()
    provisioning_request.client_id.CopyFrom(client_id)
    provisioning_request.nonce = get_random_bytes(4)
    provisioning_request.options.CopyFrom(provisioning_options)
    # some 7.x android versions might set this, but don't have examples to confirm it
    # provisioning_request.stable_id = keybox.stable_id

    provisioning_request_string = provisioning_request.SerializeToString()
    nonce = provisioning_request.nonce
    enc_key, auth_1_key, auth_2_key = generate_derived_keys(provisioning_request_string, keybox.device_aes_key)

    log.info(f"Unsigned provisioning request: {provisioning_request_string!r}")
    log.info(f"Nonce: {nonce!r}")
    log.info("Generated keys:")
    log.info(f" + enc: {enc_key}")
    log.info(f" + auth_1: {auth_1_key}")
    log.info(f" + auth_2: {auth_2_key}")

    signed_provisioning_message = SignedProvisioningMessage()
    signed_provisioning_message.message = provisioning_request_string
    signed_provisioning_message.signature = HMAC.new(
        auth_2_key, digestmod=SHA256
    ).update(provisioning_request_string).digest()
    signed_provisioning_message.protocol_version = ProtocolVersion.VERSION_2_0

    """
    there is some suspicion that VERSION_3 works by setting field id=5 (encrypted client id) in the ProvisioningRequest
    to an empty 0x101 byte buffer, because a certain recent trustzone applet's code will do protobuf parsing within
    trustzone extract field with id=5, check its size, then generate a RSA-PSS-SHA1 signature of the preceding fields
    (1-4), not including field 5 or any that follow it. field 5 would be set to: 01 || PSS-signature and have the size
    of 0x101 bytes.

    the 2048bit key used is embedded elsewhere within trustzone code (and can be easily extracted as long as the
    hardware secrets are known, in a similar way as you'd extract a keybox, but more lengthy decryption operations
    (at least 5-6 layers of AES encryption if going from the device root of trust))

    this key is presumed to be shared with at least all the models of that type, if not all widevine lvl1
    implementations once this field is filled into the message by the trustzone code, it continues with generating a
    HMAC-SHA256 signature as version 2 would the trustzone code does nothing if the field is not present or if its size
    is insufficient previous versions of the trustzone applet did not do this, and I haven't seen accompanying code in
    userspace libraries that set this buffer empty buffer, however I might have missed it, as compiled C++ protobuf
    code is somewhat ugly and I didn't want to generate all the needed structures to correctly view what exact fields
    it was setting.

    This was not implemented because of the preceding reasons and that id=5 in official proto's is a message with a
    different structure, not bytes.
    """

    signed_request = base64.urlsafe_b64encode(signed_provisioning_message.SerializeToString()).rstrip(b"=").decode()
    log.info(f"Signed provisioning message url-safe base64: {signed_request}")

    session = requests.session()
    if proxy:
        session.trust_env = False
        session.proxies = {"all": proxy}
    if user_agent:
        session.headers.update({"User-Agent": user_agent})
    else:
        session.headers.pop("User-Agent")  # no python-requests default user agent

    server_prov_response = session.post(
        "https://www.googleapis.com/certificateprovisioning/v1/devicecertificates/create",
        params={
            "key": "AIzaSyB-5OLKTx2iU5mko18DfdwK5611JIjbUhE",
            "signedRequest": signed_request
        }
    ).json()

    if "error" in server_prov_response:
        raise log.exit(
            f"Failed! Server returned error while doing provisioning request: {server_prov_response['error']}"
        )

    if server_prov_response["kind"] != "certificateprovisioning#certificateProvisioningResponse":
        raise log.exit(f"Failed! Unexpected 'kind' field in provisioning response: {server_prov_response['kind']}")

    signed_response = SignedProvisioningMessage()
    signed_response.ParseFromString(base64.urlsafe_b64decode(server_prov_response["signedResponse"]))

    response_signature_computed = HMAC.new(auth_1_key, digestmod=SHA256).update(signed_response.message).digest()
    if response_signature_computed != signed_response.signature:
        raise log.exit("Failed! Provisioning response signature is incorrect: {got!r}. Expected: {expected!r}".format(
            got=signed_response.signature, expected=response_signature_computed
        ))

    provisioning_response = ProvisioningResponse()
    provisioning_response.ParseFromString(signed_response.message)

    log.info(f"Response decoded: {provisioning_response}")

    if provisioning_response.nonce != nonce:
        raise log.exit("Failed! Response Nonce mismatched: {got!r}. Expected: {expected!r}".format(
            got=provisioning_response.nonce, expected=nonce
        ))

    ci = ClientIdentificationRaw()
    ci.Type = ClientIdentificationRaw.DEVICE_CERTIFICATE
    ci.Token = provisioning_response.device_certificate

    config["client_info"]["device_id"] = keybox.stable_id

    for name, value in config["client_info"].items():
        nv = ci.ClientInfo.add()
        nv.Name = name
        nv.Value = value

    capabilities = ClientIdentificationRaw.ClientCapabilities()
    caps = config["capabilities"]
    if "client_token" in caps:
        capabilities.ClientToken = caps["client_token"]
    if "session_token" in caps:
        capabilities.SessionToken = caps["session_token"]
    if "video_resolution_constraints" in caps:
        capabilities.VideoResolutionConstraints = caps["video_resolution_constraints"]
    if "max_hdcp_version" in caps:
        max_hdcp_version = caps["max_hdcp_version"]
        if str(max_hdcp_version).isdigit():
            max_hdcp_version = int(max_hdcp_version)
        else:
            max_hdcp_version = ClientIdentificationRaw.ClientCapabilities.HdcpVersion.Value(max_hdcp_version)
        capabilities.MaxHdcpVersion = max_hdcp_version
    if "oem_crypto_api_version" in caps:
        capabilities.OemCryptoApiVersion = int(caps["oem_crypto_api_version"])
    # I have not seen any of the following in use:
    if "anti_rollback_usage_table" in caps:
        capabilities.AntiRollbackUsageTable = caps["anti_rollback_usage_table"]
    if "srm_version" in caps:
        capabilities.SrmVersion = int(caps["srm_version"])
    if "can_update_srm" in caps:
        capabilities.ClientToken = caps["can_update_srm"]
    # is it possible to refactor this?
    if "supported_certificate_key_type" in caps:
        supported_certificate_key_type = caps["supported_certificate_key_type"]
        if str(supported_certificate_key_type).isdigit():
            supported_certificate_key_type = int(supported_certificate_key_type)
        else:
            supported_certificate_key_type = ClientIdentificationRaw.ClientCapabilities.CertificateKeyType.Value(
                supported_certificate_key_type)
        capabilities.SupportedCertificateKeyType.append(supported_certificate_key_type)
    ci._ClientCapabilities.CopyFrom(capabilities)

    log.info(f"Generated Device Certificate Client ID: {ci}")

    aes = AES.new(enc_key, AES.MODE_CBC, iv=provisioning_response.device_rsa_key_iv)
    device_rsa_key = Padding.unpad(aes.decrypt(provisioning_response.device_rsa_key), 0x10)

    out_path = os.path.splitext(keybox_path)[0] + f"_l{config['wvd']['security_level']}_{keybox.system_id}.wvd"
    LocalDevice(
        type=LocalDevice.Types[config["wvd"]["device_type"].upper()],
        system_id=keybox.system_id,
        security_level=config["wvd"]["security_level"],
        flags={
            "send_key_control_nonce": config["wvd"]["send_key_control_nonce"]
        },
        private_key=device_rsa_key,
        client_id=ci.SerializeToString(),
        vmp=None
    ).dump(out_path)

    log.info(f"Generated WVD to: {out_path}")


if __name__ == "__main__":
    prv()
