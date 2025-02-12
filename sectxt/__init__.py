#
# SPDX-License-Identifier: EUPL-1.2
#
import codecs

import langcodes
import re
import sys
from cgi import parse_header
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional, Union, List, DefaultDict
from urllib.parse import urlsplit, urlunsplit
import pgpy
from pgpy.errors import PGPError

if sys.version_info < (3, 8):
    from typing_extensions import TypedDict
else:
    from typing import TypedDict

import dateutil.parser
import requests

__version__ = "0.9.1"

s = requests.Session()


class ErrorDict(TypedDict):
    code: str
    message: str
    line: Optional[int]


class LineDict(TypedDict):
    type: str
    field_name: Optional[str]
    value: str


def strlist_from_arg(arg: Union[str, List[str], None]) -> Union[List[str], None]:
    if isinstance(arg, str):
        return [arg]
    return arg


PREFERRED_LANGUAGES = "preferred-languages"


class Parser:
    iso8601_re = re.compile(
        r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[-+]\d{2}:\d{2})$",
        re.IGNORECASE | re.ASCII,
    )

    uri_fields = [
        "acknowledgments",
        "canonical",
        "contact",
        "encryption",
        "hiring",
        "policy",
        "csaf",
    ]

    known_fields = uri_fields + [PREFERRED_LANGUAGES, "expires"]

    def __init__(
        self,
        content: str,
        urls: Optional[str] = None,
        recommend_unknown_fields: bool = True,
    ):
        self._urls = strlist_from_arg(urls)
        self._line_info: List[LineDict] = []
        self._errors: List[ErrorDict] = []
        self._recommendations: List[ErrorDict] = []
        self._notifications: List[ErrorDict] = []
        self._values: DefaultDict[str, List[str]] = defaultdict(list)
        self._langs: Optional[List[str]] = None
        self._signed = False
        self._reading_sig = False
        self._finished_sig = False
        self._content = content
        self.recommend_unknown_fields = recommend_unknown_fields
        self._line_no: Optional[int] = None
        self._process()

    def _process(self) -> None:
        lines = self._content.split("\n")
        self._line_no = 1
        for line in lines:
            self._line_info.append(self._parse_line(line))
            self._line_no += 1
        self._line_no = None
        self.validate_contents()

    def _add_error(
        self,
        code: str,
        message: str,
        explicit_line_no=None
    ) -> None:
        if explicit_line_no:
            error_line = explicit_line_no
        else:
            error_line = self._line_no
        err_dict: ErrorDict = {"code": code, "message": message, "line": error_line}
        self._errors.append(err_dict)

    def _add_recommendation(
        self,
        code: str,
        message: str,
    ) -> None:
        err_dict: ErrorDict = {"code": code, "message": message, "line": self._line_no}
        self._recommendations.append(err_dict)

    def _add_notification(
        self,
        code: str,
        message: str,
    ) -> None:
        err_dict: ErrorDict = {"code": code, "message": message, "line": self._line_no}
        self._notifications.append(err_dict)

    def _parse_line(self, line: str) -> LineDict:
        line = line.rstrip()

        if self._reading_sig:
            if line == "-----END PGP SIGNATURE-----":
                self._reading_sig = False
                self._finished_sig = True
            return {"type": "pgp_envelope", "field_name": None, "value": line}

        if line and self._finished_sig:
            self._add_error(
                "data_after_sig",
                "Signed security.txt must not contain data after the signature.",
            )
            return {"type": "error", "field_name": None, "value": line}

        # signed content might be dash escaped
        if self._signed and not self._reading_sig and line.startswith("- "):
            line = line[2:]

        if line == "-----BEGIN PGP SIGNED MESSAGE-----":
            if self._line_no != 1:
                self._add_error(
                    "signed_format_issue",
                    "Signed security.txt must start with the header "
                    "'-----BEGIN PGP SIGNED MESSAGE-----'.",
                )
            self._signed = True

            # Check pgp formatting if signed
            try:
                pgpy.PGPMessage.from_blob(self._content)
            except ValueError:
                self._add_error(
                    "pgp_data_error",
                    "Signed message did not contain a correct ASCII-armored PGP block."
                )
            except PGPError as e:
                self._add_error(
                    "pgp_error",
                    "Decoding or parsing of the pgp message failed."
                )

            return {"type": "pgp_envelope", "field_name": None, "value": line}

        if line == "-----BEGIN PGP SIGNATURE-----" and self._signed:
            self._reading_sig = True
            return {"type": "pgp_envelope", "field_name": None, "value": line}

        if line.startswith("#"):
            return {"type": "comment", "value": line, "field_name": None}

        if ":" in line:
            return self._parse_field(line)

        if line:
            self._add_error(
                "invalid_line",
                "Line must contain a field name and value, "
                "unless the line is blank or contains a comment.",
            )
            return {"type": "error", "value": line, "field_name": None}

        return {"type": "empty", "value": "", "field_name": None}

    def _parse_field(self, line: str) -> LineDict:
        key, value = line.split(":", 1)
        key = key.lower()
        if key.rstrip() != key:
            self._add_error(
                "prec_ws",
                "There must be no whitespace before the field separator (colon).",
            )
            key = key.rstrip()

        if value:
            if value[0] != " ":
                self._add_error(
                    "no_space", "Field separator (colon) must be followed by a space."
                )
            value = value.lstrip()

        if key == "hash" and self._signed:
            return {"type": "pgp_envelope", "field_name": None, "value": line}

        if not key:
            self._add_error("empty_key", "Field name must not be empty.")
            return {"type": "error", "value": line, "field_name": None}

        if not value:
            self._add_error("empty_value", "Field value must not be empty.")
            return {"type": "error", "value": line, "field_name": None}

        if key in self.uri_fields:
            url_parts = urlsplit(value)
            if url_parts.scheme == "":
                self._add_error(
                    "no_uri",
                    f"Field '{key}' value must be a URI.",
                )
            elif url_parts.scheme == "http":
                self._add_error("no_https", "Web URI must begin with 'https://'.")
        elif key == "expires":
            self._parse_expires(value)
        elif key == PREFERRED_LANGUAGES:
            self._langs = [v.strip() for v in value.split(",")]

            # Check if all the languages are valid according to RFC5646.
            for lang in self._langs:
                if not langcodes.tag_is_valid(lang):
                    self._add_error(
                        "invalid_lang",
                        "Value in 'Preferred-Languages' field must match one "
                        "or more language tags as defined in RFC5646, "
                        "separated by commas.",
                    )

        if self.recommend_unknown_fields and key not in self.known_fields:
            self._add_notification(
                "unknown_field",
                "security.txt contains an unknown field. "
                'Field "%s" is either a custom field which may not be widely '
                "supported, or there is a typo in a standardised field name." % key,
            )

        self._values[key].append(value)
        return {"type": "field", "field_name": key, "value": value}

    def _parse_expires(self, value: str) -> None:
        try:
            date_value = dateutil.parser.parse(value)
        except dateutil.parser.ParserError:
            self._add_error(
                "invalid_expiry",
                "Date and time in 'Expires' field must be formatted "
                "according to ISO 8601.",
            )
        else:
            self._expires_date = date_value
            if not self.iso8601_re.match(value):
                # dateutil parses more than just iso8601 format
                self._add_error(
                    "invalid_expiry",
                    "Date and time in 'Expires' field must be formatted "
                    "according to ISO 8601.",
                )
                # Stop to prevent errors when comparing the current datetime,
                # which is set with a timezone, and the parsed date, that
                # could potentially not have a timezone.
                return

            now = datetime.now(timezone.utc)
            max_value = now.replace(year=now.year + 1)
            if date_value > max_value:
                self._add_recommendation(
                    "long_expiry",
                    "Date and time in 'Expires' field should be less than "
                    "a year into the future.",
                )
            elif date_value < now:
                self._add_error(
                    "expired",
                    "Date and time in 'Expires' field must not be in the past.",
                )

    def validate_contents(self) -> None:
        if "expires" not in self._values:
            self._add_error("no_expire", "'Expires' field must be present.")
        elif len(self._values["expires"]) > 1:
            self._add_error(
                "multi_expire", "'Expires' field must not appear more than once."
            )
        if self._urls and "canonical" in self._values:
            if all(url not in self._values["canonical"] for url in self._urls):
                self._add_error(
                    "no_canonical_match",
                    "Web URI where security.txt is located must match with a "
                    "'Canonical' field. In case of redirecting either the "
                    "first or last web URI of the redirect chain must match.",
                )
        if self.lines[-1]["type"] != "empty":
            self._add_error(
                "no_line_separators",
                "Every line, including the last one, must end with "
                "either a carriage return and line feed characters "
                "or just a line feed character",
                len(self.lines)
            )

        if "csaf" in self._values:
            if not all(
                v.endswith("provider-metadata.json") for v in self._values["csaf"]
            ):
                self._add_error(
                    "no_csaf_file",
                    "All CSAF fields must point to a provider-metadata.json file.",
                )
            if len(self._values["csaf"]) > 1:
                self._add_recommendation(
                    "multiple_csaf_fields",
                    "It is allowed to have more than one csaf field, "
                    "however this should be removed if possible.",
                )

        if "contact" not in self._values:
            self._add_error("no_contact", "'Contact' field must appear at least once.")
        else:
            if (
                any(v.startswith("mailto:") for v in self._values["contact"])
                and "encryption" not in self._values
            ):
                self._add_recommendation(
                    "no_encryption",
                    "'Encryption' field should be present when 'Contact' "
                    "field contains an email address.",
                )
        if PREFERRED_LANGUAGES in self._values:
            if len(self._values[PREFERRED_LANGUAGES]) > 1:
                self._add_error(
                    "multi_lang",
                    "'Preferred-Languages' field must not appear more than once.",
                )

        if not self._signed:
            self._add_recommendation(
                "not_signed", "security.txt should be digitally signed."
            )
        if self._signed and not self._values.get("canonical"):
            self._add_recommendation(
                "no_canonical", "'Canonical' field should be present in a signed file."
            )

    def is_valid(self) -> bool:
        return not self._errors

    @property
    def errors(self) -> List[ErrorDict]:
        return self._errors

    @property
    def recommendations(self) -> List[ErrorDict]:
        return self._recommendations

    @property
    def notifications(self) -> List[ErrorDict]:
        return self._notifications

    @property
    def lines(self) -> List[LineDict]:
        return self._line_info

    @property
    def preferred_languages(self) -> Union[List[str], None]:
        if PREFERRED_LANGUAGES in self._values:
            return [v.strip() for v in self._values[PREFERRED_LANGUAGES][0].split(",")]
        return None

    @property
    def contact_email(self) -> Union[None, str]:
        if "contact" in self._values:
            for value in self._values["contact"]:
                if value.startswith("mailto:"):
                    return value[7:]
                if ":" not in value and "@" in value:
                    return value
        return None

    @property
    def resolved_url(self) -> Optional[str]:
        if self._urls:
            return self._urls[-1]
        return None


CORRECT_PATH = ".well-known/security.txt"


class SecurityTXT(Parser):
    def __init__(self, url: str, recommend_unknown_fields: bool = True):
        url_parts = urlsplit(url)
        if url_parts.scheme:
            if not url_parts.netloc:
                raise ValueError("Invalid URL")
            netloc = url_parts.netloc
        else:
            netloc = url
        self._netloc = netloc
        self._path: Optional[str] = None
        self._url: Optional[str] = None
        super().__init__("", recommend_unknown_fields=recommend_unknown_fields)

    def _get_str(self, content: bytes) -> str:
        try:
            if content.startswith(codecs.BOM_UTF8):
                content = content.replace(codecs.BOM_UTF8, b'')
            self._add_error(
                "bom_in_file",
                "The Byte-Order Mark was found in the UTF-8 File. "
                "Security.txt must be encoded using UTF-8 in Net-Unicode form, "
                "the BOM signature must not appear at the beginning."
            )
            return content.decode('utf-8')
        except UnicodeError:
            self._add_error("utf8", "Content must be utf-8 encoded.")
        return content.decode('utf-8', errors="replace")

    def _process(self) -> None:
        security_txt_found = False
        for scheme in ["https", "http"]:
            for path in [".well-known/security.txt", "security.txt"]:
                url = urlunsplit((scheme, self._netloc, path, None, None))
                try:
                    resp = requests.get(
                        url,
                        headers={
                            'User-Agent': 'Mozilla/5.0 (Windows NT 6.1; WOW64; rv:12.0) '
                                          'Gecko/20100101 Firefox/12.0'},
                        timeout=5
                    )
                except requests.exceptions.SSLError:
                    if not any(d["code"] == "invalid_cert" for d in self._errors):
                        self._add_error(
                            "invalid_cert",
                            "security.txt must be served with a valid TLS certificate.",
                        )
                    try:
                        resp = requests.get(
                            url,
                            headers={
                                'User-Agent': 'Mozilla/5.0 (Windows NT 6.1; WOW64; rv:12.0) '
                                              'Gecko/20100101 Firefox/12.0'},
                            timeout=5,
                            verify=False
                        )
                    except:
                        continue
                except:
                    continue
                if resp.status_code == 200:
                    self._path = path
                    self._url = url
                    if scheme != "https":
                        self._add_error(
                            "invalid_uri_scheme",
                            "Insecure URI scheme HTTP is not allowed. "
                            "The security.txt file access MUST use "
                            'the "https" scheme',
                        )
                    if path != CORRECT_PATH:
                        self._add_error(
                            "location",
                            "security.txt was located on the top-level path "
                            "(legacy place), but must be placed under "
                            "the '/.well-known/' path.",
                        )
                    if "content-type" not in resp.headers:
                        self._add_error(
                            "no_content_type", "HTTP Content-Type header must be sent."
                        )
                    else:
                        media_type, params = parse_header(resp.headers["content-type"])
                        if media_type.lower() != "text/plain":
                            self._add_error(
                                "invalid_media",
                                "Media type in Content-Type header must be "
                                "'text/plain'.",
                            )
                        charset = params.get("charset", "utf-8").lower()
                        if charset != "utf-8" and charset != "csutf8":
                            # According to RFC9116, charset default is utf-8
                            self._add_error(
                                "invalid_charset",
                                "Charset parameter in Content-Type header must be "
                                "'utf-8' if present.",
                            )
                    self._content = self._get_str(resp.content)
                    if resp.history:
                        self._urls = [resp.history[0].url, resp.url]
                    else:
                        self._urls = [url]
                    super()._process()
                    security_txt_found = True
                    break
            if security_txt_found:
                break
        if not security_txt_found:
            self._add_error("no_security_txt", "security.txt could not be located.")
