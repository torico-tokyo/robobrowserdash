"""
Robotic browser.
"""

import re
import os
import base64
import pickle

import requests
from bs4 import BeautifulSoup
try:
    from functools import cached_property
except ImportError:
    from werkzeug.utils import cached_property
from requests.packages.urllib3.util.retry import Retry

from robobrowser import helpers
from robobrowser import exceptions
from robobrowser.compat import urlparse
from robobrowser.forms.form import Form
from robobrowser.cache import RoboHTTPAdapter


_link_ptn = re.compile(r'^(a|button)$', re.I)
_form_ptn = re.compile(r'^form$', re.I)


class RoboState:
    """Representation of a browser state. Wraps the browser and response, and
    lazily parses the response content.

    """

    def __init__(self, browser, response):
        self.browser = browser
        self.response = response
        self.url = response.url if isinstance(response.url, str) else str(response.url)

    @cached_property
    def parsed(self):
        """Lazily parse response content, using HTML parser specified by the
        browser.
        """
        return BeautifulSoup(
            self.response.content,
            features=self.browser.parser,
        )


class RoboBrowser:
    """Robotic web browser. Represents HTTP requests and responses using the
    requests library and parsed HTML using BeautifulSoup.

    :param str parser: HTML parser; used by BeautifulSoup
    :param str user_agent: Default user-agent
    :param history: History length; infinite if True, 1 if falsy, else
        takes integer value

    :param int timeout: Default timeout, in seconds
    :param bool allow_redirects: Allow redirects on POST/PUT/DELETE

    :param bool cache: Cache responses
    :param list cache_patterns: List of URL patterns for cache
    :param timedelta max_age: Max age for cache
    :param int max_count: Max count for cache

    :param int tries: Number of retries
    :param Exception errors: Exception or tuple of exceptions to catch
    :param int delay: Delay between retries
    :param int multiplier: Delay multiplier between retries

    """
    def __init__(self, *, session=None, parser=None, user_agent=None,
                 history=True, timeout=None, allow_redirects=True, cache=False,
                 cache_patterns=None, max_age=None, max_count=None, tries=None,
                 send_referer=True,
                 multiplier=None, asynchronously=False):
        if session:
            self.session = session
        else:
            self.session = RoboBrowser.create_async_session() if asynchronously else requests.Session()

        # Add default user agent string
        if user_agent is not None:
            self.session.headers['User-Agent'] = user_agent

        self.parser = parser or 'lxml'

        self.timeout = timeout
        self.allow_redirects = allow_redirects
        self.send_referer = send_referer
        self.simple_cookie = None

        # Set up caching
        if cache:
            adapter = RoboHTTPAdapter(max_age=max_age, max_count=max_count)
            cache_patterns = cache_patterns or ['http://', 'https://']
            for pattern in cache_patterns:
                self.session.mount(pattern, adapter)
        elif max_age:
            raise ValueError('Parameter `max_age` is provided, '
                             'but caching is turned off')
        elif max_count:
            raise ValueError('Parameter `max_count` is provided, '
                             'but caching is turned off')

        # Configure history
        self.history = history
        if history is True:
            self._maxlen = None
        elif not history:
            self._maxlen = 1
        else:
            self._maxlen = history
        self._states = []
        self._cursor = -1

        # Set up retries
        if tries:
            retry = Retry(tries, backoff_factor=multiplier)
            for protocol in ['http://', 'https://']:
                self.session.adapters[protocol].max_retries = retry

    def __repr__(self):
        try:
            return '<RoboBrowser url={0}>'.format(self.url)
        except exceptions.RoboError:
            return '<RoboBrowser>'

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.session.close()

    @staticmethod
    def create_async_session():
        import aiohttp
        return aiohttp.ClientSession()

    @classmethod
    def acreate(cls, session=None, **kwargs) -> 'RoboBrowser':
        """
        async with RoboBrowser.acreate() as browser:
            await browser.aopen(url)
        """
        import aiohttp
        session = session or aiohttp.ClientSession()
        return cls(session=session, **kwargs)

    @property
    def state(self):
        if self._cursor == -1:
            raise exceptions.RoboError('No state')
        try:
            return self._states[self._cursor]
        except IndexError:
            raise exceptions.RoboError('Index out of range')

    @property
    def response(self):
        return self.state.response

    @property
    def url(self):
        return self.state.url

    @property
    def parsed(self):
        return self.state.parsed

    @property
    def find(self):
        """See ``BeautifulSoup::find``."""
        try:
            return self.parsed.find
        except AttributeError:
            raise exceptions.RoboError

    @property
    def find_all(self):
        """See ``BeautifulSoup::find_all``."""
        try:
            return self.parsed.find_all
        except AttributeError:
            raise exceptions.RoboError

    @property
    def select(self):
        """See ``BeautifulSoup::select``."""
        try:
            return self.parsed.select
        except AttributeError:
            raise exceptions.RoboError

    def _build_url(self, url):
        """Build absolute URL.

        :param url: Full or partial URL
        :return: Full URL

        """
        return urlparse.urljoin(
            self.url,
            url
        )

    def set_proxy(self, proxies):
        self.session.proxies = proxies

    @property
    def _default_send_args(self):
        """

        """
        return {
            'timeout': self.timeout,
            'allow_redirects': self.allow_redirects,
        }

    def _build_send_args(self, **kwargs):
        """Merge optional arguments with defaults.

        :param kwargs: Keyword arguments to `Session::send`

        """
        out = {}
        out.update(self._default_send_args)
        out.update(kwargs)
        return out

    def open(self, url, method='get', **kwargs):
        """Open a URL.

        :param str url: URL to open
        :param str method: Optional method; defaults to `'get'`
        :param kwargs: Keyword arguments to `Session::request`

        """
        response = self.session.request(method, url, **self._build_send_args(**kwargs))
        self._update_state(response)

    async def aopen(self, url, method='get', encoding=None, **kwargs):
        """Open a URL as asynchronously.
        e.g
        with aiohttp.ClientSession() as client:
            browser = RoboBrowser(session=client)
            await browser.aopen(url)
        """
        if self.session.closed:
            raise exceptions.SessionClosedError('session is already closed')

        async with getattr(self.session, method)(url, **self._build_send_args(**kwargs)) as resp:
            content = await resp.read()
            text = await resp.text(encoding)
        resp.content = content
        resp.text = text
        self._update_state(resp)

    def _update_state(self, response):
        """Update the state of the browser. Create a new state object, and
        append to or overwrite the browser's state history.

        :param requests.MockResponse: New response object

        """
        # Clear trailing states
        self._states = self._states[:self._cursor + 1]

        # Append new state
        state = RoboState(self, response)
        self._states.append(state)
        self._cursor += 1

        # Clear leading states
        if self._maxlen:
            decrement = len(self._states) - self._maxlen
            if decrement > 0:
                self._states = self._states[decrement:]
                self._cursor -= decrement

    def _traverse(self, n=1):
        """Traverse state history. Used by `back` and `forward` methods.

        :param int n: Cursor increment. Positive values move forward in the
            browser history; negative values move backward.

        """
        if not self.history:
            raise exceptions.RoboError('Not tracking history')
        cursor = self._cursor + n
        if cursor >= len(self._states) or cursor < 0:
            raise exceptions.RoboError('Index out of range')
        self._cursor = cursor

    def back(self, n=1):
        """Go back in browser history.

        :param int n: Number of pages to go back

        """
        self._traverse(-1 * n)

    def forward(self, n=1):
        """Go forward in browser history.

        :param int n: Number of pages to go forward

        """
        self._traverse(n)

    def get_link(self, text=None, *args, **kwargs):
        """Find an anchor or button by containing text, as well as standard
        BeautifulSoup arguments.

        :param text: String or regex to be matched in link text
        :return: BeautifulSoup tag if found, else None

        """
        return helpers.find(
            self.parsed, _link_ptn, text=text, *args, **kwargs
        )

    def get_links(self, text=None, *args, **kwargs):
        """Find anchors or buttons by containing text, as well as standard
        BeautifulSoup arguments.

        :param text: String or regex to be matched in link text
        :return: List of BeautifulSoup tags

        """
        return helpers.find_all(
            self.parsed, _link_ptn, text=text, *args, **kwargs
        )

    def get_form(self, id=None, *args, **kwargs):
        """Find form by ID, as well as standard BeautifulSoup arguments.

        :param str id: Form ID
        :return: BeautifulSoup tag if found, else None

        """
        if id:
            kwargs['id'] = id
        form = self.find(_form_ptn, *args, **kwargs)
        if form is not None:
            return Form(form)

    def get_forms(self, *args, **kwargs):
        """Find forms by standard BeautifulSoup arguments.
        :args: Positional arguments to `BeautifulSoup::find_all`
        :args: Keyword arguments to `BeautifulSoup::find_all`

        :return: List of BeautifulSoup tags

        """
        forms = self.find_all(_form_ptn, *args, **kwargs)
        return [
            Form(form)
            for form in forms
        ]

    def follow_link(self, link, **kwargs):
        """Click a link.

        :param Tag link: Link to click
        :param kwargs: Keyword arguments to `Session::send`

        """
        try:
            href = link['href']
        except KeyError:
            raise exceptions.RoboError('Link element must have "href" '
                                       'attribute')
        self.open(self._build_url(href), **kwargs)

    def submit_form(self, form, submit=None, **kwargs):
        """Submit a form.

        :param Form form: Filled-out form object
        :param Submit submit: Optional `Submit` to click, if form includes
            multiple submits
        :param kwargs: Keyword arguments to `Session::send`

        """
        # Get HTTP verb
        method = form.method.upper()

        if self.url and self.send_referer:
            kwargs.setdefault('headers', {})
            kwargs['headers'].setdefault('Referer', self.url)

        # Send request
        url = self._build_url(form.action) or self.url
        payload = form.serialize(submit=submit)
        serialized = payload.to_requests(method)
        send_args = self._build_send_args(**kwargs)
        send_args.update(serialized)
        response = self.session.request(method, url, **send_args)

        # Update history
        self._update_state(response)

    # Dash extra methods
    def get_cookies(self):
        """
        :return: http.cookiejar.Cookie list
        For serialize, example django cache
        """
        return list(iter(self.session.cookies))

    def get_serialized_cookies(self):
        b = pickle.dumps(self.get_cookies())
        return base64.b64encode(b).decode()

    def save_cookies_to_file(self, file_path):
        """
        Save pickled cookies to file
        """
        with open(file_path, 'wb') as fp:
            pickle.dump(self.get_cookies(), fp)

    def set_cookies(self, cookies):
        """
        From serialized, example django cache
        """
        for cookie in cookies:
            self.session.cookies.set_cookie(cookie)

    def set_serialized_cookies(self, text):
        b = base64.b64decode(text)
        cookies = pickle.loads(b)
        self.set_cookies(cookies)

    def load_cookies_from_file(self, file_path, fail_silently=True):
        """
        Load unpickled cookies to file
        """
        if not os.path.exists(file_path) and fail_silently:
            return
        with open(file_path, 'rb') as fp:
            cookies = pickle.load(fp)
            self.set_cookies(cookies)

    def get_cookies_as_dicts(self):
        """
        Get cookies for debug
        """
        return [c.__dict__ for c in iter(self.session.cookies)]

    def get_cookie_values_as_dicts(self):
        """
        Get cookies for eazy debug.
        """
        return [
            {
                "domain": c.domain,
                "name": c.name,
                "value": c.value,
            } for c in iter(self.session.cookies)
        ]

    def preview(self, suffix='.html'):
        """
        Preview latest response with GUI web browser
        """
        import tempfile
        import subprocess

        f = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        f.write(self.state.response.content)
        f.close()

        # for mac.
        subprocess.Popen(['open', f.name])
        # Auto delete required?

    def get_decoded_content(self):
        """
        Get html content as str
        """
        return self.response.text

    def take_snapshot(self, file_path):
        """
        take html snapshot to file
        """
        with open(file_path, "wb") as fp:
            fp.write(self.state.response.content)

    re_meta_refresh_content_url = re.compile(r'url=(.+)', re.I)

    re_http_equiv_refresh = re.compile("^refresh$", re.I)

    def meta_refresh(self):
        """
        Execute meta refresh.
        """
        meta_refresh = self.find(
            'meta', attrs={'http-equiv': self.re_http_equiv_refresh})
        if not meta_refresh:
            return

        content = meta_refresh['content']
        if not content:
            return

        match = self.re_meta_refresh_content_url.search(content)
        if not match:
            return

        self.open(match.group(1))

    def load_html(self, html, **kwargs):
        """
        Load html force. Overwrite on current state.
        """
        kwargs.setdefault('features', 'lxml')
        parsed = BeautifulSoup(html, **kwargs)
        self.state.parsed = parsed

    def get_parsed_url(self):
        """
        Get parsed current url.
        """
        return urlparse.urlparse(self.url)

    def get_parsed_query(self, flatten=True, *kwargs):
        """
        Get parsed current url queries
        """
        parsed_url = self.get_parsed_url()
        qs = urlparse.parse_qs(parsed_url.query, *kwargs)
        if not flatten:
            return qs
        return {
            k: v if len(v) >= 2 else v[0]
            for k, v in qs.items()
        }

    def reparse(self, decode=True, encoding=None, errors='ignore'):
        """
        Retry parse content for beautifulsoup
        """
        from bs4 import BeautifulSoup
        if decode:
            if encoding:
                content = self.state.response.content.decode(
                    encoding, errors=errors)
            else:
                content = self.state.response.content.decode(
                    errors=errors)
        else:
            content = self.state.response.content
        self.state.parsed = BeautifulSoup(content, features=self.parser)

    def header_encoding(self):
        match = re.search(
            'charset=(.+)',
            self.response.headers.get('Content-Type', ''),
            re.IGNORECASE)
        if match:
            return match.group(1).lower()

    async def aclose(self):
        try:
            await self.session.close()
        except TypeError:
            pass
