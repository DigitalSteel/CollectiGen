"""Local stock urllib opener with explicit first-party URL admission.

No global opener, proxy mutation, trust enrollment or automatic retries. CDN
origins must be supplied by the publisher/entry owner after qualification.
"""
from dataclasses import dataclass
from urllib.parse import urlsplit, parse_qs, unquote
from urllib.request import Request, HTTPRedirectHandler, ProxyHandler, build_opener

from suite_core.discovery.github_component_releases import REPOSITORY, release_asset_identity


class TransportHold(ValueError):
    pass


def _https(url):
    if type(url) is not str or not url or any(ord(c) < 33 for c in url) or "\\" in url:
        raise TransportHold("invalid_release_url")
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "https" and parsed.username is None
                 and parsed.password is None and parsed.port in (None, 443)
                 and parsed.hostname and not parsed.fragment)
    except ValueError as error:
        raise TransportHold("invalid_release_url") from error
    if not valid:
        raise TransportHold("https_release_origin_required")
    decoded = unquote(parsed.path)
    if (any(ord(c) < 32 for c in decoded) or "\\" in decoded
            or any(part in (".", "..") for part in decoded.split("/"))):
        raise TransportHold("release_path_traversal")
    return parsed


@dataclass(frozen=True)
class ReleaseTransportPolicy:
    asset_redirect_origins: tuple[str, ...] = ()
    max_redirects: int = 4
    schema2_metadata_assets: tuple[str, ...] = ()

    def __post_init__(self):
        if type(self.schema2_metadata_assets) is not tuple or self.schema2_metadata_assets not in (
                (),('collectigen-release.json','collectigen-release.json.sig')):
            raise TransportHold("canonical_schema2_metadata_assets")
        if type(self.max_redirects) is not int or not 0 <= self.max_redirects <= 8:
            raise TransportHold("redirect_bound")
        if type(self.asset_redirect_origins) is not tuple:
            raise TransportHold("explicit_redirect_origins_required")
        for origin in self.asset_redirect_origins:
            parsed = _https(origin)
            if (parsed.path not in ("", "/") or parsed.query
                    or parsed.netloc != parsed.hostname or "*" in origin
                    or parsed.hostname in ("api.github.com", "github.com")):
                raise TransportHold("exact_asset_redirect_origin_required")

    def admit_initial(self, url):
        parsed = _https(url)
        if url == f"https://api.github.com/repos/{REPOSITORY}":
            return "repository"
        if parsed.netloc == "api.github.com" and parsed.path == f"/repos/{REPOSITORY}/releases":
            query = parse_qs(parsed.query, strict_parsing=True)
            if set(query) == {"per_page", "page"} and query["per_page"] == ["50"] and query["page"] in (["1"], ["2"], ["3"]):
                return "listing"
        try:
            release_asset_identity(url)
        except ValueError:
            pass
        else:
            return "asset"
        if self.schema2_metadata_assets:
            try:
                tag,name=release_asset_identity(url,include_schema2=True)
            except ValueError:
                pass
            else:
                if '/releases/schema2/' in tag and name in self.schema2_metadata_assets:
                    return 'asset'
        raise TransportHold("first_party_release_locator_required")

    def admit_redirect(self, initial_url, url, hops):
        if hops > self.max_redirects:
            raise TransportHold("release_redirect_bound")
        kind = self.admit_initial(initial_url)
        parsed = _https(url)
        if kind in ("listing", "repository"):
            # API relocation is not silently accepted as a changed repository.
            if url != initial_url:
                raise TransportHold("release_listing_redirect_held")
        elif url != initial_url and f"https://{parsed.netloc}" not in self.asset_redirect_origins:
            raise TransportHold("unqualified_asset_redirect_origin")


class _ReleaseRedirects(HTTPRedirectHandler):
    def __init__(self, policy):
        self.policy = policy

    def redirect_request(self, request, response, code, message, headers, newurl):
        original = getattr(request, "_release_initial", request.full_url)
        hops = getattr(request, "_release_hops", 0) + 1
        self.policy.admit_redirect(original, newurl, hops)
        redirected = super().redirect_request(request, response, code, message, headers, newurl)
        if redirected is not None:
            redirected._release_initial, redirected._release_hops = original, hops
            # Never forward caller credentials/cookies to an asset CDN.
            for name in tuple(redirected.headers) + tuple(redirected.unredirected_hdrs):
                if name.lower() in ("authorization", "proxy-authorization", "cookie"):
                    redirected.remove_header(name)
        return redirected


class ComponentReleaseTransport:
    def __init__(self, policy=None):
        self.policy = policy or ReleaseTransportPolicy()
        # Deliberate direct transport, normal HTTPS certificate verification.
        self._opener = build_opener(ProxyHandler({}), _ReleaseRedirects(self.policy))

    def __call__(self, request, *, timeout):
        if not isinstance(request, Request):
            raise TransportHold("release_request_required")
        self.policy.admit_initial(request.full_url)
        if request.get_method() != "GET" or request.data is not None:
            raise TransportHold("release_get_required")
        if any(name.lower() in ("authorization", "proxy-authorization", "cookie")
               for name, _ in request.header_items()):
            raise TransportHold("credential_free_release_request_required")
        if type(timeout) not in (int, float) or not 0 < timeout <= 60:
            raise TransportHold("bounded_transport_timeout_required")
        request._release_initial, request._release_hops = request.full_url, 0
        return self._opener.open(request, timeout=timeout)
