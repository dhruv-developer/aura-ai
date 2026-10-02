"""SAML 2.0 service-provider side: SP-initiated AuthnRequest (HTTP-Redirect binding)
and strict validation of the IdP's Response (HTTP-POST binding).

Security notes:
- Signatures are verified with signxml against the connection's pinned IdP
  certificate, and **only the element signxml returns as signed is read**. Reading the
  original document instead is how XML-signature-wrapping attacks work.
- Audience, recipient, issuer, validity window and InResponseTo are all checked. The
  caller enforces one-time use of the assertion id (replay protection).
"""

from __future__ import annotations

import base64
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from xml.sax.saxutils import escape

from lxml import etree
from signxml.exceptions import InvalidInput, InvalidSignature
from signxml.verifier import XMLVerifier

from aura_shared.errors import Unauthenticated

NS = {
    "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
    "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
}
SKEW = timedelta(minutes=3)
EMAIL_ATTRIBUTES = (
    "email",
    "mail",
    "emailaddress",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
    "urn:oid:0.9.2342.19200300.100.1.3",
)
NAME_ATTRIBUTES = (
    "name",
    "displayname",
    "http://schemas.microsoft.com/identity/claims/displayname",
)


@dataclass(frozen=True, slots=True)
class SamlIdentity:
    assertion_id: str
    name_id: str
    email: str
    name: str | None
    not_on_or_after: datetime


def authn_request_url(
    *, idp_sso_url: str, sp_entity_id: str, acs_url: str, request_id: str, relay_state: str
) -> str:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    xml = (
        f'<samlp:AuthnRequest xmlns:samlp="{NS["samlp"]}" xmlns:saml="{NS["saml"]}" '
        f'ID="{escape(request_id)}" Version="2.0" IssueInstant="{now}" '
        f'Destination="{escape(idp_sso_url)}" AssertionConsumerServiceURL="{escape(acs_url)}" '
        'ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST">'
        f"<saml:Issuer>{escape(sp_entity_id)}</saml:Issuer>"
        '<samlp:NameIDPolicy Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress" '
        'AllowCreate="true"/></samlp:AuthnRequest>'
    )
    deflater = zlib.compressobj(9, zlib.DEFLATED, -15)  # raw DEFLATE, per the binding
    payload = base64.b64encode(deflater.compress(xml.encode()) + deflater.flush()).decode()
    sep = "&" if "?" in idp_sso_url else "?"
    return f"{idp_sso_url}{sep}{urlencode({'SAMLRequest': payload, 'RelayState': relay_state})}"


def _time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _fail(reason: str) -> Unauthenticated:
    return Unauthenticated(f"SAML response rejected: {reason}")


def parse_response(
    saml_response_b64: str,
    *,
    idp_entity_id: str,
    idp_cert_pem: str,
    sp_entity_id: str,
    acs_url: str,
    expected_request_id: str,
    now: datetime | None = None,
) -> SamlIdentity:
    now = now or datetime.now(UTC)
    try:
        raw = base64.b64decode(saml_response_b64, validate=True)
    except ValueError as exc:
        raise _fail("not base64") from exc
    try:
        results = XMLVerifier().verify(raw, x509_cert=idp_cert_pem)
    except (InvalidSignature, InvalidInput, etree.XMLSyntaxError) as exc:
        raise _fail("signature invalid") from exc
    verified = results if not isinstance(results, list) else results[0]
    signed = verified.signed_xml
    if signed is None:
        raise _fail("nothing signed")
    tag = etree.QName(signed).localname
    if tag == "Assertion":
        assertion = signed
    elif tag == "Response":
        found = signed.findall("saml:Assertion", NS)
        if len(found) != 1:
            raise _fail("expected exactly one assertion")
        assertion = found[0]
        status = signed.find("samlp:Status/samlp:StatusCode", NS)
        if status is None or not str(status.get("Value", "")).endswith(":Success"):
            raise _fail("IdP reported failure")
    else:
        raise _fail(f"unexpected signed element {tag}")

    issuer = assertion.findtext("saml:Issuer", namespaces=NS)
    if (issuer or "").strip() != idp_entity_id:
        raise _fail("issuer mismatch")
    cond = assertion.find("saml:Conditions", NS)
    if cond is None:
        raise _fail("no conditions")
    nb, noa = _time(cond.get("NotBefore")), _time(cond.get("NotOnOrAfter"))
    if (nb and now + SKEW < nb) or (noa and now - SKEW >= noa):
        raise _fail("assertion expired or not yet valid")
    audiences = [a.text for a in cond.findall("saml:AudienceRestriction/saml:Audience", NS)]
    if sp_entity_id not in audiences:
        raise _fail("audience mismatch")
    subject = assertion.find("saml:Subject", NS)
    if subject is None:
        raise _fail("no subject")
    confirmed = False
    for sc in subject.findall("saml:SubjectConfirmation", NS):
        data = sc.find("saml:SubjectConfirmationData", NS)
        if sc.get("Method") != "urn:oasis:names:tc:SAML:2.0:cm:bearer" or data is None:
            continue
        exp = _time(data.get("NotOnOrAfter"))
        if (
            data.get("Recipient") == acs_url
            and data.get("InResponseTo") == expected_request_id
            and exp is not None
            and now - SKEW < exp
        ):
            confirmed = True
    if not confirmed:
        raise _fail("subject confirmation failed (recipient, request or expiry)")

    name_id = (subject.findtext("saml:NameID", namespaces=NS) or "").strip()
    attrs: dict[str, str] = {}
    for attr in assertion.findall("saml:AttributeStatement/saml:Attribute", NS):
        value = attr.findtext("saml:AttributeValue", namespaces=NS)
        if value:
            attrs[str(attr.get("Name", "")).lower()] = value.strip()
    email = next((attrs[a] for a in EMAIL_ATTRIBUTES if a in attrs), None)
    if email is None and "@" in name_id:
        email = name_id
    if not email:
        raise _fail("no email in assertion")
    assertion_id = assertion.get("ID")
    if not assertion_id:
        raise _fail("assertion has no ID")
    return SamlIdentity(
        assertion_id=assertion_id,
        name_id=name_id or email,
        email=email,
        name=next((attrs[a] for a in NAME_ATTRIBUTES if a in attrs), None),
        not_on_or_after=noa or now + timedelta(minutes=5),
    )


def sp_metadata(*, sp_entity_id: str, acs_url: str) -> str:
    return (
        '<?xml version="1.0"?>'
        '<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" '
        f'entityID="{escape(sp_entity_id)}"><md:SPSSODescriptor '
        'AuthnRequestsSigned="false" WantAssertionsSigned="true" '
        'protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
        "<md:NameIDFormat>urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress</md:NameIDFormat>"
        '<md:AssertionConsumerService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
        f'Location="{escape(acs_url)}" index="0"/></md:SPSSODescriptor></md:EntityDescriptor>'
    )
