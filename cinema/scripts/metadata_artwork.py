"""Download one metadata cover and validate the image before publishing it."""
import base64
import hashlib
import io
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit
import warnings

MAX_IMAGE_BYTES = 10_000_000
MAX_PAYLOAD_BYTES = 14_000_000
FORMATS = {'JPEG': 'jpg', 'PNG': 'png', 'WEBP': 'webp'}
# Surge/Clash can resolve known public providers into the fake-IP range.
# Only these observed provider hosts may use that range; never private/LAN IPs.
PROVIDER_FAKE_DNS_HOSTS = {'www.javbus.com', 'javbus.com', 'javtrailers.com', 'images.javtrailers.com'}
FAKE_DNS_RANGE = ipaddress.ip_network('198.18.0.0/15')


def inspect_image(raw):
    from PIL import Image
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ValueError('Cover exceeds the 10 MB limit or is empty')
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(raw)) as image:
            extension = FORMATS.get(image.format)
            width, height = image.size
            if not extension or min(width, height) < 64 or width * height > 25_000_000:
                raise ValueError('Unsupported cover format or dimensions')
            if getattr(image, 'is_animated', False):
                raise ValueError('Cover must be a still image')
            image.verify()
        with Image.open(io.BytesIO(raw)) as image:
            image.load()  # Detect truncated images, not just valid headers.
    return {'extension': extension, 'width': width, 'height': height,
            'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def image_payload(raw, source):
    return {**inspect_image(raw), 'source': source,
            'data': base64.b64encode(raw).decode('ascii')}


def decode_image(payload):
    encoded = payload.get('data', '')
    if not isinstance(encoded, str) or len(encoded) > MAX_PAYLOAD_BYTES:
        raise ValueError('Invalid cover payload size')
    raw = base64.b64decode(encoded, validate=True)
    details = inspect_image(raw)
    if any(payload.get(field) != value for field, value in details.items()):
        raise ValueError('Cover checksum, format or dimensions do not match')
    if payload.get('source') not in ('javbus', 'javtrailers', 'local'):
        raise ValueError('Unexpected cover source')
    return raw, details


def public_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('Cover URL must use public HTTPS')
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    def allowed_address(address):
        ip = ipaddress.ip_address(address[4][0])
        return ip.is_global or (parsed.hostname in PROVIDER_FAKE_DNS_HOSTS and ip in FAKE_DNS_RANGE)
    if not addresses or any(not allowed_address(address) for address in addresses):
        raise ValueError('Cover URL must resolve to a public address')
    return url


async def fetch_cover(candidates):
    import httpx
    failures = []
    async with httpx.AsyncClient(timeout=25, follow_redirects=False, headers={
        'User-Agent': 'Mozilla/5.0', 'Accept': 'image/jpeg,image/png,image/webp',
    }) as client:
        for candidate in candidates:
            source = candidate['source']
            try:
                url = public_url(candidate['url'])
                headers = {'Referer': public_url(candidate['referer'])} if candidate.get('referer') else {}
                for _ in range(4):
                    async with client.stream('GET', url, headers=headers) as response:
                        if response.is_redirect:
                            url = public_url(urljoin(url, response.headers['location']))
                            continue
                        response.raise_for_status()
                        if int(response.headers.get('content-length', 0)) > MAX_IMAGE_BYTES:
                            raise ValueError('Cover is too large')
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw) > MAX_IMAGE_BYTES:
                                raise ValueError('Cover is too large')
                        return image_payload(bytes(raw), source), failures
                raise ValueError('Too many cover redirects')
            except Exception as error:
                # Do not echo provider response bodies or signed URLs.
                failures.append({'source': source, 'error_type': type(error).__name__})
    raise RuntimeError('No usable cover available: ' + str(failures))
