"""HTML text and same-site link selection; never executes page scripts."""
from urllib.parse import unquote, urljoin, urlsplit
from bs4 import BeautifulSoup
from .security import normalize_url

LINK_WORDS = ('about', 'product', 'service', 'contact', 'team', 'company', 'unternehmen', 'kontakt',
              'produkte', 'servicios', 'nosotros', 'contacto', '关于', '联系', '产品', '服务', '会社', 'お問い合わせ')


def clean_html(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, 'html.parser')
    for node in soup(['script', 'style', 'noscript', 'iframe', 'template', 'svg', 'form']):
        node.decompose()
    for node in soup.select('[hidden], [aria-hidden="true"]'):
        node.decompose()
    return soup


def page_content(html: str, url: str, allowed_hosts: tuple[str, ...], *, fixture=False) -> tuple[str, list[str], list[str]]:
    soup = clean_html(html)
    internal, external = set(), set()
    for link in soup.find_all('a', href=True):
        try:
            href = normalize_url(urljoin(url, link['href']), fixture=fixture)
        except Exception:
            continue
        if urlsplit(href).path.lower().endswith(('.pdf', '.zip', '.exe', '.doc', '.docx', '.xlsx', '.png', '.jpg')):
            continue
        (internal if urlsplit(href).hostname in allowed_hosts else external).add(href)
    return soup.get_text(' ', strip=True), sorted(internal), sorted(external)


def rank_links(links: list[str]) -> list[str]:
    def key(url):
        path = unquote(urlsplit(url).path).lower()
        return (0 if any(word in path for word in LINK_WORDS) else 1, len(path), url)
    # Unknown paths are bounded too; they cannot manufacture more depth/pages.
    return sorted(set(links), key=key)
