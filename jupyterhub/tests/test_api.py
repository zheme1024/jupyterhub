"""Tests for the REST API."""

import asyncio
import json
import re
import sys
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from unittest import mock
from urllib.parse import parse_qs, quote, urlparse

import pytest
from dateutil.parser import parse as parse_date
from pytest import fixture, mark
from tornado.httputil import url_concat

import jupyterhub

from .. import orm
from ..apihandlers.base import PAGINATION_MEDIA_TYPE
from ..objects import Server
from ..utils import url_path_join as ujoin
from ..utils import utcnow
from .conftest import new_username
from .utils import (
    add_user,
    api_request,
    async_requests,
    auth_header,
    find_user,
    public_host,
    public_url,
)

# --------------------
# Authentication tests
# --------------------


async def test_auth_api(app):
    db = app.db
    r = await api_request(app, 'authorizations', 'gobbledygook')
    assert r.status_code == 404

    # make a new cookie token
    user = find_user(db, 'admin')
    api_token = user.new_api_token()

    # check success:
    r = await api_request(app, 'authorizations/token', api_token)
    assert r.status_code == 200
    reply = r.json()
    assert reply['name'] == user.name

    # check fail
    r = await api_request(
        app, 'authorizations/token', api_token, headers={'Authorization': 'no sir'}
    )
    assert r.status_code == 403

    r = await api_request(
        app,
        'authorizations/token',
        api_token,
        headers={'Authorization': f'token: {user.cookie_id}'},
    )
    assert r.status_code == 403


@mark.parametrize(
    "content_type, status",
    [
        ("text/plain", 403),
        # accepted, but invalid
        ("application/json; charset=UTF-8", 400),
    ],
)
async def test_post_content_type(app, content_type, status):
    url = ujoin(public_host(app), app.hub.base_url)
    host = urlparse(url).netloc
    # add admin user
    user = find_user(app.db, 'admin')
    if user is None:
        user = add_user(app.db, name='admin', admin=True)
    cookies = await app.login_user('admin')

    r = await api_request(
        app,
        'users',
        method='post',
        data='{}',
        headers={
            "Authorization": "",
            "Content-Type": content_type,
        },
        cookies=cookies,
    )
    assert r.status_code == status


@mark.parametrize("xsrf_in_url", [True, False, "invalid"])
@mark.parametrize(
    "method, path",
    [
        ("GET", "user"),
        ("POST", "users/{username}/tokens"),
    ],
)
async def test_xsrf_check(app, username, method, path, xsrf_in_url):
    cookies = await app.login_user(username)
    xsrf = cookies['_xsrf']
    if xsrf_in_url == "invalid":
        cookies.pop("_xsrf")
        # a valid old-style tornado xsrf token is no longer valid
        xsrf = cookies['_xsrf'] = (
            "2|7329b149|d837ced983e8aac7468bc7a61ce3d51a|1708610065"
        )

    url = path.format(username=username)
    if xsrf_in_url:
        url = f"{url}?_xsrf={xsrf}"

    r = await api_request(
        app,
        url,
        noauth=True,
        cookies=cookies,
    )
    if xsrf_in_url is True:
        assert r.status_code == 200
    else:
        assert r.status_code == 403


@mark.parametrize(
    "auth, expected_message",
    [
        ("", "Missing or invalid credentials"),
        ("cookie_no_xsrf", "'_xsrf' argument missing from GET"),
        ("cookie_xsrf_mismatch", "XSRF cookie does not match GET argument"),
        ("token_no_scope", "requires any of [list:users]"),
        ("cookie_no_scope", "requires any of [list:users]"),
    ],
)
async def test_permission_error_messages(app, user, auth, expected_message):
    # 1. no credentials, should be 403 and not mention xsrf

    url = public_url(app, path="hub/api/users")

    kwargs = {}
    kwargs["headers"] = headers = {}
    kwargs["params"] = params = {}
    if auth == "token_no_scope":
        token = user.new_api_token()
        headers["Authorization"] = f"Bearer {token}"
    elif "cookie" in auth:
        cookies = kwargs["cookies"] = await app.login_user(user.name)
        if auth == "cookie_no_scope":
            params["_xsrf"] = cookies["_xsrf"]
        if auth == "cookie_xsrf_mismatch":
            params["_xsrf"] = "somethingelse"
    headers['Sec-Fetch-Mode'] = 'cors'
    r = await async_requests.get(url, **kwargs)
    assert r.status_code == 403
    response = r.json()
    message = response["message"]
    assert expected_message in message


# --------------
# User API tests
# --------------


def normalize_timestamp(ts):
    """Normalize a timestamp

    For easier comparison
    """
    if ts is None:
        return
    return re.sub(r'\d(\.\d+)?', '0', ts)


def normalize_user(user):
    """Normalize a user model for comparison

    smooths out user model with things like timestamps
    for easier comparison
    """
    for key in ('created', 'last_activity'):
        user[key] = normalize_timestamp(user[key])
    if 'roles' in user:
        user['roles'] = sorted(user['roles'])
    if 'servers' in user:
        for server in user['servers'].values():
            for key in ('started', 'last_activity'):
                server[key] = normalize_timestamp(server[key])
            server['progress_url'] = re.sub(
                r'.*/hub/api', 'PREFIX/hub/api', server['progress_url']
            )
            if isinstance(server['state'], dict) and isinstance(
                server['state'].get('pid', None), int
            ):
                server['state']['pid'] = 0
    return user


def fill_user(model):
    """Fill a default user model

    Any unspecified fields will be filled with the defaults
    """
    model.setdefault('server', None)
    model.setdefault('kind', 'user')
    model.setdefault('roles', [])
    model.setdefault('groups', [])
    model.setdefault('admin', False)
    model.setdefault('pending', None)
    model.setdefault('created', TIMESTAMP)
    model.setdefault('last_activity', TIMESTAMP)
    model.setdefault('servers', {})
    return model


TIMESTAMP = normalize_timestamp(datetime.now().isoformat() + 'Z')


@mark.user
@mark.role
async def test_get_users(app):
    db = app.db

    r = await api_request(app, 'users', headers=auth_header(db, 'admin'))
    assert r.status_code == 200

    users = sorted(r.json(), key=lambda d: d['name'])
    users = [normalize_user(u) for u in users]
    user_model = {
        'name': 'user',
        'admin': False,
        'roles': ['user'],
        'auth_state': None,
    }
    assert users == [
        fill_user(
            {
                'name': 'admin',
                'admin': True,
                'roles': ['admin', 'user'],
                'auth_state': None,
            }
        ),
        fill_user(user_model),
    ]
    r = await api_request(app, 'users', headers=auth_header(db, 'user'))
    assert r.status_code == 403


@fixture
def default_page_limit(app):
    """Set and return low default page size for testing"""
    n = 10
    with mock.patch.dict(app.tornado_settings, {"api_page_default_limit": n}):
        yield n


@fixture
def max_page_limit(app):
    """Set and return low max page size for testing"""
    n = 20
    with mock.patch.dict(app.tornado_settings, {"api_page_max_limit": n}):
        yield n


@mark.user
@mark.parametrize(
    "n, offset, limit, accepts_pagination, expected_count, include_stopped_servers",
    [
        (10, None, None, False, 10, False),
        (10, None, None, True, 10, False),
        (10, 5, None, True, 5, False),
        (10, 5, None, False, 5, False),
        (10, None, 5, True, 5, True),
        (10, 5, 1, True, 1, True),
        (10, 10, 10, True, 0, False),
        (  # default page limit, pagination expected
            30,
            None,
            None,
            True,
            'default',
            False,
        ),
        (
            # default max page limit, pagination not expected
            30,
            None,
            None,
            False,
            'max',
            False,
        ),
        (
            # limit exceeded
            30,
            None,
            500,
            False,
            'max',
            False,
        ),
    ],
)
async def test_get_users_pagination(
    app,
    n,
    offset,
    limit,
    accepts_pagination,
    expected_count,
    default_page_limit,
    max_page_limit,
    include_stopped_servers,
):
    db = app.db

    if expected_count == 'default':
        expected_count = default_page_limit
    elif expected_count == 'max':
        expected_count = max_page_limit
    # populate users
    usernames = []

    groups = []
    for i in range(3):
        group = orm.Group(name=f"pagination-{i}")
        db.add(group)
    db.commit()
    existing_users = db.query(orm.User).order_by(orm.User.id.asc())
    usernames.extend(u.name for u in existing_users)

    for i in range(n - existing_users.count()):
        name = new_username()
        usernames.append(name)
        user = add_user(db, app, name=name)
        # add some users to groups
        # make sure group membership doesn't affect pagination count
        if i % 2:
            user.groups = groups
    db.commit()

    total_users = db.query(orm.User).count()

    url = 'users'
    params = {}
    if offset:
        params['offset'] = offset
    if limit:
        params['limit'] = limit
    url = url_concat(url, params)
    if include_stopped_servers:
        # assumes limit is set. There doesn't seem to be a way to set valueless query
        # params using url_cat
        url += "&include_stopped_servers"

    headers = auth_header(db, 'admin')
    if accepts_pagination:
        headers['Accept'] = PAGINATION_MEDIA_TYPE
    r = await api_request(app, url, headers=headers)
    assert r.status_code == 200
    response = r.json()
    if accepts_pagination:
        assert set(response) == {
            "items",
            "_pagination",
        }
        pagination = response["_pagination"]
        if include_stopped_servers and pagination["next"]:
            next_query = parse_qs(
                urlparse(pagination["next"]["url"]).query, keep_blank_values=True
            )
            assert "include_stopped_servers" in next_query
        users = response["items"]
        assert pagination["total"] == total_users
    else:
        users = response
    assert len(users) == expected_count
    expected_usernames = usernames
    if offset:
        expected_usernames = expected_usernames[offset:]
    expected_usernames = expected_usernames[:expected_count]

    got_usernames = [u['name'] for u in users]
    assert got_usernames == expected_usernames


@mark.user
@mark.parametrize(
    "state",
    ("inactive", "active", "ready", "invalid"),
)
async def test_get_users_state_filter(app, state):
    db = app.db

    # has_one_active: one active, one inactive, zero ready
    has_one_active = add_user(db, app=app, name='has_one_active')
    # has_two_active: two active, ready servers
    has_two_active = add_user(db, app=app, name='has_two_active')
    # has_two_inactive: two spawners, neither active
    has_two_inactive = add_user(db, app=app, name='has_two_inactive')
    # has_zero: no Spawners registered at all
    has_zero = add_user(db, app=app, name='has_zero')
    total_users = db.query(orm.User).count()

    test_usernames = {
        "has_one_active",
        "has_two_active",
        "has_two_inactive",
        "has_zero",
    }

    user_states = {
        "inactive": ["has_two_inactive", "has_zero"],
        "ready": ["has_two_active"],
        "active": ["has_one_active", "has_two_active"],
        "invalid": [],
    }
    expected = user_states[state]

    def add_spawner(user, name='', active=True, ready=True):
        """Add a spawner in a requested state

        If active, should turn up in an active query
        If active and ready, should turn up in a ready query
        If not active, should turn up in an inactive query
        """
        spawner = user.spawners[name]
        db.commit()
        if active:
            orm_server = orm.Server()
            db.add(orm_server)
            db.commit()
            spawner.server = Server(orm_server=orm_server)
            db.commit()
            if not ready:
                spawner._spawn_pending = True
        return spawner

    for name in ("", "secondary"):
        add_spawner(has_two_active, name, active=True)
        add_spawner(has_two_inactive, name, active=False)

    add_spawner(has_one_active, active=True, ready=False)
    add_spawner(has_one_active, "inactive", active=False)

    r = await api_request(
        app, f'users?state={state}', headers={"Accept": PAGINATION_MEDIA_TYPE}
    )
    if state == "invalid":
        assert r.status_code == 400
        return
    assert r.status_code == 200

    response = r.json()
    users = response["items"]
    page = response["_pagination"]

    usernames = sorted(u["name"] for u in users if u["name"] in test_usernames)
    assert usernames == expected
    if state == "ready":
        # "ready" can't actually get a correct count because it has post-filtering applied
        # but it has an upper bound
        assert page["total"] >= len(users)
    else:
        assert page["total"] == len(users)


@mark.user
async def test_get_users_name_filter(app):
    db = app.db

    add_user(db, app=app, name='q')
    add_user(db, app=app, name='qr')
    add_user(db, app=app, name='qrs')
    add_user(db, app=app, name='qrst')
    added_usernames = {'q', 'qr', 'qrs', 'qrst'}

    r = await api_request(app, 'users')
    assert r.status_code == 200
    response_users = [u.get("name") for u in r.json()]
    assert added_usernames.intersection(response_users) == added_usernames

    r = await api_request(app, 'users?name_filter=q')
    assert r.status_code == 200
    response_users = [u.get("name") for u in r.json()]
    assert response_users == ['q', 'qr', 'qrs', 'qrst']

    r = await api_request(app, 'users?name_filter=qr')
    assert r.status_code == 200
    response_users = [u.get("name") for u in r.json()]
    assert response_users == ['qr', 'qrs', 'qrst']

    r = await api_request(app, 'users?name_filter=qrs')
    assert r.status_code == 200
    response_users = [u.get("name") for u in r.json()]
    assert response_users == ['qrs', 'qrst']

    r = await api_request(app, 'users?name_filter=qrst')
    assert r.status_code == 200
    response_users = [u.get("name") for u in r.json()]
    assert response_users == ['qrst']


@mark.user
@pytest.mark.parametrize("direction", ["asc", "desc"])
@pytest.mark.parametrize("sort", ["id", "name", "last_activity"])
async def test_get_users_sort(app, sort, direction):
    db = app.db

    # 4 users, different order depending on sort field
    orders = {
        "id": ["1", "2", "3", "4"],
        "name": ["a", "b", "c", "d"],
        "last_activity": ["never", "early", "middle", "late"],
    }
    expected_order = orders[sort]
    sort_param = sort
    if direction == "desc":
        expected_order.reverse()
        sort_param = "-" + sort

    # create the users, encode the expected sort order in the names
    u = add_user(db, app=app, name='xyz-c-1-middle')
    u.last_activity = utcnow() - timedelta(hours=1)
    u = add_user(db, app=app, name='xyz-a-2-late')
    u.last_activity = utcnow()
    u = add_user(db, app=app, name='xyz-d-3-never')
    u.last_activity = None
    u = add_user(db, app=app, name='xyz-b-4-early')
    u.last_activity = utcnow() - timedelta(days=1)
    app.db.commit()

    @dataclass
    class UserName:
        """Parse username so we can get the current sort field"""

        id: str
        name: str
        last_activity: str

        def __init__(self, username):
            prefix, self.name, self.id, self.last_activity = username.split("-")

    # fetch 4 users in 2 pages of 2 items each
    # to ensure offset is handled correctly
    params = {
        "name_filter": "xyz",
        "sort": sort_param,
        "limit": 2,
    }

    r = await api_request(
        app, 'users', params=params, headers={"Accept": PAGINATION_MEDIA_TYPE}
    )
    assert r.status_code == 200
    page_1 = r.json()

    assert page_1["_pagination"]["total"] == 4
    users = page_1["items"]
    assert len(users) == 2

    # next page
    params["offset"] = page_1["_pagination"]["next"]["offset"]
    r = await api_request(
        app, 'users', params=params, headers={"Accept": PAGINATION_MEDIA_TYPE}
    )
    assert r.status_code == 200
    page_2 = r.json()

    # turn user dicts into list of only the relevant component,
    # e.g. { "name": "xyz-a-2-late" } -> "late"
    users.extend(page_2["items"])
    usernames = [UserName(u["name"]) for u in users]
    sorted_fields = [getattr(u, sort) for u in usernames]
    assert sorted_fields == expected_order


async def test_get_users_sort_invalid(app):
    r = await api_request(app, "users", params={"sort": "servers"})
    assert r.status_code == 400
    r = await api_request(app, "users", params={"sort": "--id"})
    assert r.status_code == 400


@mark.user
async def test_get_self(app):
    db = app.db

    # basic get self
    r = await api_request(app, 'user')
    r.raise_for_status()
    assert r.json()['kind'] == 'user'

    # identifying user via oauth token works
    u = add_user(db, app=app, name='orpheus')
    token = uuid.uuid4().hex
    oauth_client = orm.OAuthClient(identifier='eurydice')
    db.add(oauth_client)
    db.commit()
    oauth_token = orm.APIToken(
        token=token,
    )
    db.add(oauth_token)
    oauth_token.user = u.orm_user
    oauth_token.oauth_client = oauth_client

    db.commit()
    r = await api_request(
        app,
        'user',
        headers={'Authorization': 'token ' + token},
    )
    r.raise_for_status()
    model = r.json()
    assert model['name'] == u.name
    assert model["token_id"] == oauth_token.api_id

    # invalid auth gets 403
    r = await api_request(
        app,
        'user',
        headers={'Authorization': 'token notvalid'},
    )
    assert r.status_code == 403


async def test_get_self_service(app, mockservice):
    r = await api_request(
        app, "user", headers={"Authorization": f"token {mockservice.api_token}"}
    )
    r.raise_for_status()
    service_info = r.json()

    assert service_info['kind'] == 'service'
    assert service_info['name'] == mockservice.name


@mark.user
@mark.role
async def test_add_user(app):
    db = app.db
    name = 'newuser'
    r = await api_request(app, 'users', name, method='post')
    assert r.status_code == 201
    user = find_user(db, name)
    assert user is not None
    assert user.name == name
    assert not user.admin
    # assert newuser has default 'user' role
    assert orm.Role.find(db, 'user') in user.roles
    assert orm.Role.find(db, 'admin') not in user.roles


@mark.user
@mark.role
async def test_get_user(app):
    name = 'user'
    # get own model
    r = await api_request(app, 'users', name, headers=auth_header(app.db, name))
    r.raise_for_status()
    # admin request
    r = await api_request(
        app,
        'users',
        name,
    )
    r.raise_for_status()

    user = normalize_user(r.json())
    assert user == fill_user({'name': name, 'roles': ['user'], 'auth_state': None})

    # admin request, no such user
    r = await api_request(
        app,
        'users',
        'nosuchuser',
    )
    assert r.status_code == 404

    # unauthorized request, no such user
    r = await api_request(
        app,
        'users',
        'nosuchuser',
        headers=auth_header(app.db, name),
    )
    assert r.status_code == 404

    # unauthorized request for existing user
    r = await api_request(
        app,
        'users',
        'admin',
        headers=auth_header(app.db, name),
    )
    assert r.status_code == 404


@mark.user
async def test_add_multi_user_bad(app):
    r = await api_request(app, 'users', method='post')
    assert r.status_code == 400
    r = await api_request(app, 'users', method='post', data='{}')
    assert r.status_code == 400
    r = await api_request(app, 'users', method='post', data='[]')
    assert r.status_code == 400


@mark.user
async def test_add_multi_user_invalid(app):
    app.authenticator.username_pattern = r'w.*'
    r = await api_request(
        app,
        'users',
        method='post',
        data=json.dumps({'usernames': ['Willow', 'Andrew', 'Tara']}),
    )
    app.authenticator.username_pattern = ''
    assert r.status_code == 400
    assert r.json()['message'] == 'Invalid usernames: andrew, tara'


@mark.user
@mark.role
async def test_add_multi_user(app):
    db = app.db
    names = ['a', 'b']
    r = await api_request(
        app, 'users', method='post', data=json.dumps({'usernames': names})
    )
    assert r.status_code == 201
    reply = r.json()
    r_names = [user['name'] for user in reply]
    assert names == r_names

    for name in names:
        user = find_user(db, name)
        assert user is not None
        assert user.name == name
        assert not user.admin
        # assert default 'user' role added
        assert orm.Role.find(db, 'user') in user.roles
        assert orm.Role.find(db, 'admin') not in user.roles

    # try to create the same users again
    r = await api_request(
        app, 'users', method='post', data=json.dumps({'usernames': names})
    )
    assert r.status_code == 409

    names = ['a', 'b', 'ab']

    # try to create the same users again
    r = await api_request(
        app, 'users', method='post', data=json.dumps({'usernames': names})
    )
    assert r.status_code == 201
    reply = r.json()
    r_names = [user['name'] for user in reply]
    assert r_names == ['ab']


@mark.user
@mark.role
@mark.parametrize("is_admin", [True, False])
async def test_add_multi_user_admin(app, create_user_with_scopes, is_admin):
    db = app.db
    requester = create_user_with_scopes("admin:users")
    requester.admin = is_admin
    db.commit()
    names = ['c', 'd']
    r = await api_request(
        app,
        'users',
        method='post',
        data=json.dumps({'usernames': names, 'admin': True}),
        name=requester.name,
    )
    if is_admin:
        assert r.status_code == 201
    else:
        assert r.status_code == 403
        return
    reply = r.json()
    r_names = [user['name'] for user in reply]
    assert names == r_names

    for name in names:
        user = find_user(db, name)
        assert user is not None
        assert user.name == name
        assert user.admin
        assert orm.Role.find(db, 'user') in user.roles
        assert orm.Role.find(db, 'admin') in user.roles


@mark.user
async def test_add_user_bad(app):
    db = app.db
    name = 'dne_newuser'
    r = await api_request(app, 'users', name, method='post')
    assert r.status_code == 400
    user = find_user(db, name)
    assert user is None


@mark.user
async def test_add_user_duplicate(app):
    db = app.db
    name = 'user'
    user = find_user(db, name)
    # double-check that it exists
    assert user is not None
    r = await api_request(app, 'users', name, method='post')
    # special 409 conflict for creating a user that already exists
    assert r.status_code == 409


@mark.user
@mark.role
@mark.parametrize("is_admin", [True, False])
async def test_add_admin(app, create_user_with_scopes, is_admin):
    db = app.db
    name = 'newadmin'
    user = create_user_with_scopes("admin:users")
    user.admin = is_admin
    db.commit()
    r = await api_request(
        app,
        'users',
        name,
        method='post',
        data=json.dumps({'admin': True}),
        name=user.name,
    )
    if is_admin:
        assert r.status_code == 201
    else:
        assert r.status_code == 403
        return
    user = find_user(db, name)
    assert user is not None
    assert user.name == name
    assert user.admin
    # assert newadmin has default 'admin' role
    assert orm.Role.find(db, 'user') in user.roles
    assert orm.Role.find(db, 'admin') in user.roles


@mark.user
async def test_delete_user(app):
    db = app.db
    mal = add_user(db, name='mal')
    r = await api_request(app, 'users', 'mal', method='delete')
    assert r.status_code == 204


@mark.user
@mark.role
@mark.parametrize("is_admin", [True, False])
async def test_user_make_admin(app, create_user_with_scopes, is_admin):
    db = app.db
    requester = create_user_with_scopes('admin:users')
    requester.admin = is_admin
    db.commit()

    name = new_username("make_admin")
    r = await api_request(app, 'users', name, method='post')
    assert r.status_code == 201
    user = find_user(db, name)
    assert user is not None
    assert user.name == name
    assert not user.admin
    assert orm.Role.find(db, 'user') in user.roles
    assert orm.Role.find(db, 'admin') not in user.roles

    r = await api_request(
        app,
        'users',
        name,
        method='patch',
        data=json.dumps({'admin': True}),
        name=requester.name,
    )
    if is_admin:
        assert r.status_code == 200
    else:
        assert r.status_code == 403
        return
    user = find_user(db, name)
    assert user is not None
    assert user.name == name
    assert user.admin
    assert orm.Role.find(db, 'user') in user.roles
    assert orm.Role.find(db, 'admin') in user.roles


@mark.user
@mark.parametrize("requester_is_admin", [True, False])
@mark.parametrize("user_is_admin", [True, False])
async def test_user_set_name(
    app, user, create_user_with_scopes, requester_is_admin, user_is_admin
):
    db = app.db
    requester = create_user_with_scopes('admin:users')
    requester.admin = requester_is_admin
    user.admin = user_is_admin
    db.commit()
    new_name = new_username()

    r = await api_request(
        app,
        'users',
        user.name,
        method='patch',
        data=json.dumps({'name': new_name}),
        name=requester.name,
    )
    if requester_is_admin or not user_is_admin:
        assert r.status_code == 200
    else:
        assert r.status_code == 403
        return
    renamed = find_user(db, new_name)
    assert renamed is not None
    assert renamed.name == new_name
    assert renamed.id == user.id


@mark.user
async def test_set_auth_state(app, auth_state_enabled):
    auth_state = {'secret': 'hello'}
    db = app.db
    name = 'admin'
    user = find_user(db, name, app=app)
    assert user is not None
    assert user.name == name

    r = await api_request(
        app, 'users', name, method='patch', data=json.dumps({'auth_state': auth_state})
    )

    assert r.status_code == 200
    users_auth_state = await user.get_auth_state()
    assert users_auth_state == auth_state


@mark.user
async def test_user_set_auth_state(app, auth_state_enabled):
    auth_state = {'secret': 'hello'}
    db = app.db
    name = 'user'
    user = find_user(db, name, app=app)
    assert user is not None
    assert user.name == name
    user_auth_state = await user.get_auth_state()
    assert user_auth_state is None
    r = await api_request(
        app,
        'users',
        name,
        method='patch',
        data=json.dumps({'auth_state': auth_state}),
        headers=auth_header(app.db, name),
    )
    assert r.status_code == 403
    user_auth_state = await user.get_auth_state()
    assert user_auth_state is None


@mark.user
async def test_admin_get_auth_state(app, auth_state_enabled):
    auth_state = {'secret': 'hello'}
    db = app.db
    name = 'admin'
    user = find_user(db, name, app=app)
    assert user is not None
    assert user.name == name
    await user.save_auth_state(auth_state)

    r = await api_request(app, 'users', name)

    assert r.status_code == 200
    assert r.json()['auth_state'] == auth_state


@mark.user
async def test_user_get_auth_state(app, auth_state_enabled):
    # explicitly check that a user will not get their own auth state via the API
    auth_state = {'secret': 'hello'}
    db = app.db
    name = 'user'
    user = find_user(db, name, app=app)
    assert user is not None
    assert user.name == name
    await user.save_auth_state(auth_state)

    r = await api_request(app, 'users', name, headers=auth_header(app.db, name))

    assert r.status_code == 200
    assert 'auth_state' not in r.json()


async def test_spawn(app):
    db = app.db
    name = 'wash'
    user = add_user(db, app=app, name=name)
    options = {'s': ['value'], 'i': 5}
    before_servers = sorted(db.query(orm.Server), key=lambda s: s.url)
    r = await api_request(
        app, 'users', name, 'server', method='post', data=json.dumps(options)
    )
    assert r.status_code == 201
    assert 'pid' in user.orm_spawners[''].state
    app_user = app.users[name]
    assert app_user.spawner is not None
    spawner = app_user.spawner
    assert app_user.spawner.user_options == options
    assert not app_user.spawner._spawn_pending
    status = await app_user.spawner.poll()
    assert status is None

    assert spawner.server.base_url == ujoin(app.base_url, f'user/{name}') + '/'
    url = public_url(app, user)
    kwargs = {}
    if app.internal_ssl:
        kwargs['cert'] = (app.internal_ssl_cert, app.internal_ssl_key)
        kwargs["verify"] = app.internal_ssl_ca
    r = await async_requests.get(url, **kwargs)
    assert r.status_code == 200
    assert r.text == spawner.server.base_url

    r = await async_requests.get(ujoin(url, 'args'), **kwargs)
    assert r.status_code == 200
    argv = r.json()
    assert '--port' not in ' '.join(argv)
    # we pass no CLI args anymore:
    assert len(argv) == 1
    r = await async_requests.get(ujoin(url, 'env'), **kwargs)
    env = r.json()
    for expected in [
        'JUPYTERHUB_USER',
        'JUPYTERHUB_BASE_URL',
        'JUPYTERHUB_API_TOKEN',
        'JUPYTERHUB_SERVICE_URL',
    ]:
        assert expected in env
    if app.subdomain_host:
        assert env['JUPYTERHUB_HOST'] == app.subdomain_host

    r = await api_request(app, 'users', name, 'server', method='delete')
    assert r.status_code == 204

    assert 'pid' not in user.orm_spawners[''].state
    status = await app_user.spawner.poll()
    assert status == 0

    # check that we cleaned up after ourselves
    assert spawner.server is None
    after_servers = sorted(db.query(orm.Server), key=lambda s: s.url)
    assert before_servers == after_servers
    tokens = list(db.query(orm.APIToken).filter(orm.APIToken.user_id == user.id))
    assert tokens == []
    assert app.users.count_active_users()['pending'] == 0


async def test_user_options(app, username):
    db = app.db
    name = username
    user = add_user(db, app=app, name=name)
    options = {'s': ['value'], 'i': 5}
    before_servers = sorted(db.query(orm.Server), key=lambda s: s.url)
    r = await api_request(
        app, 'users', name, 'server', method='post', data=json.dumps(options)
    )
    assert r.status_code == 201
    assert 'pid' in user.orm_spawners[''].state
    app_user = app.users[name]
    assert app_user.spawner is not None
    spawner = app_user.spawner
    assert spawner.user_options == options
    assert spawner.orm_spawner.user_options == options

    # stop the server
    r = await api_request(app, 'users', name, 'server', method='delete')

    # orm_spawner still exists and has a reference to the user_options
    assert spawner.orm_spawner.user_options == options

    # spawn again, no options specified
    # should re-use options from last spawn
    r = await api_request(app, 'users', name, 'server', method='post')
    assert r.status_code == 201
    assert 'pid' in user.orm_spawners[''].state
    app_user = app.users[name]
    assert app_user.spawner is not None
    spawner = app_user.spawner
    assert spawner.user_options == options

    # stop the server
    r = await api_request(app, 'users', name, 'server', method='delete')

    # spawn again, new options specified
    # should override options from last spawn
    new_options = {'key': 'value'}
    r = await api_request(
        app, 'users', name, 'server', method='post', data=json.dumps(new_options)
    )
    assert r.status_code == 201
    assert 'pid' in user.orm_spawners[''].state
    app_user = app.users[name]
    assert app_user.spawner is not None
    spawner = app_user.spawner
    assert spawner.user_options == new_options
    # saved in db
    assert spawner.orm_spawner.user_options == new_options


async def test_spawn_handler(app):
    """Test that the requesting Handler is passed to Spawner.handler"""
    db = app.db
    name = 'salmon'
    user = add_user(db, app=app, name=name)
    app_user = app.users[name]

    # spawn via API with ?foo=bar
    r = await api_request(
        app, 'users', name, 'server', method='post', params={'foo': 'bar'}
    )
    r.raise_for_status()

    # verify that request params got passed down
    # implemented in MockSpawner
    kwargs = {}
    if app.external_certs:
        kwargs['verify'] = app.external_certs['files']['ca']
    url = public_url(app, user)
    r = await async_requests.get(ujoin(url, 'env'), **kwargs)
    env = r.json()
    assert 'HANDLER_ARGS' in env
    assert env['HANDLER_ARGS'] == 'foo=bar'
    # make user spawner.handler doesn't persist after spawn finishes
    assert app_user.spawner.handler is None

    r = await api_request(app, 'users', name, 'server', method='delete')
    r.raise_for_status()


@mark.slow
async def test_slow_spawn(app, no_patience, slow_spawn):
    db = app.db
    name = 'zoe'
    app_user = add_user(db, app=app, name=name)
    r = await api_request(app, 'users', name, 'server', method='post')
    r.raise_for_status()
    assert r.status_code == 202
    assert app_user.spawner is not None
    assert app_user.spawner._spawn_pending
    assert not app_user.spawner._stop_pending
    assert app.users.count_active_users()['pending'] == 1

    async def wait_spawn():
        while not app_user.running:
            await asyncio.sleep(0.1)

    await wait_spawn()
    assert not app_user.spawner._spawn_pending
    status = await app_user.spawner.poll()
    assert status is None

    async def wait_stop():
        while app_user.spawner._stop_pending:
            await asyncio.sleep(0.1)

    r = await api_request(app, 'users', name, 'server', method='delete')
    r.raise_for_status()
    assert r.status_code == 202
    assert app_user.spawner is not None
    assert app_user.spawner._stop_pending

    r = await api_request(app, 'users', name, 'server', method='delete')
    r.raise_for_status()
    assert r.status_code == 202
    assert app_user.spawner is not None
    assert app_user.spawner._stop_pending

    await wait_stop()
    assert not app_user.spawner._stop_pending
    assert app_user.spawner is not None
    r = await api_request(app, 'users', name, 'server', method='delete')
    # 204 deleted if there's no such server
    assert r.status_code == 204
    assert app.users.count_active_users()['pending'] == 0
    assert app.users.count_active_users()['active'] == 0


async def test_never_spawn(app, no_patience, never_spawn):
    db = app.db
    name = 'badger'
    app_user = add_user(db, app=app, name=name)
    r = await api_request(app, 'users', name, 'server', method='post')
    assert app_user.spawner is not None
    assert app_user.spawner._spawn_pending
    assert app.users.count_active_users()['pending'] == 1

    while app_user.spawner.pending:
        await asyncio.sleep(0.1)
        print(app_user.spawner.pending)

    assert not app_user.spawner._spawn_pending
    status = await app_user.spawner.poll()
    assert status is not None
    # failed spawn should decrement pending count
    assert app.users.count_active_users()['pending'] == 0


async def test_bad_spawn(app, bad_spawn):
    db = app.db
    name = 'prim'
    user = add_user(db, app=app, name=name)
    r = await api_request(app, 'users', name, 'server', method='post')
    # check that we don't re-use spawners that failed
    user.spawners[''].reused = True
    assert r.status_code == 500
    assert app.users.count_active_users()['pending'] == 0

    r = await api_request(app, 'users', name, 'server', method='post')
    # check that we don't re-use spawners that failed
    spawner = user.spawners['']
    assert not getattr(spawner, 'reused', False)


async def test_spawn_nosuch_user(app):
    r = await api_request(app, 'users', "nosuchuser", 'server', method='post')
    assert r.status_code == 404


async def test_slow_bad_spawn(app, no_patience, slow_bad_spawn):
    db = app.db
    name = 'zaphod'
    user = add_user(db, app=app, name=name)
    r = await api_request(app, 'users', name, 'server', method='post')
    r.raise_for_status()
    while user.spawner.pending:
        await asyncio.sleep(0.1)
    # spawn failed
    assert not user.running
    assert app.users.count_active_users()['pending'] == 0


def next_event(it):
    """read an event from an eventstream"""
    while True:
        try:
            line = next(it)
        except StopIteration:
            return
        if line.startswith('data:'):
            return json.loads(line.split(':', 1)[1])


@mark.slow
async def test_progress(request, app, no_patience, slow_spawn):
    db = app.db
    name = 'martin'
    app_user = add_user(db, app=app, name=name)
    r = await api_request(app, 'users', name, 'server', method='post')
    r.raise_for_status()
    r = await api_request(app, 'users', name, 'server/progress', stream=True)
    r.raise_for_status()
    request.addfinalizer(r.close)
    assert r.headers['content-type'] == 'text/event-stream'

    ex = async_requests.executor
    line_iter = iter(r.iter_lines(decode_unicode=True))
    evt = await ex.submit(next_event, line_iter)
    assert evt == {'progress': 0, 'message': 'Server requested'}
    evt = await ex.submit(next_event, line_iter)
    assert evt == {'progress': 50, 'message': 'Spawning server...'}
    evt = await ex.submit(next_event, line_iter)
    url = app_user.url
    assert evt == {
        'progress': 100,
        'message': f'Server ready at {url}',
        'html_message': f'Server ready at <a href="{url}">{url}</a>',
        'url': url,
        'ready': True,
    }


async def test_progress_not_started(request, app):
    db = app.db
    name = 'nope'
    app_user = add_user(db, app=app, name=name)
    r = await api_request(app, 'users', name, 'server', method='post')
    r.raise_for_status()
    r = await api_request(app, 'users', name, 'server', method='delete')
    r.raise_for_status()
    r = await api_request(app, 'users', name, 'server/progress')
    assert r.status_code == 404


async def test_progress_not_found(request, app):
    db = app.db
    name = 'noserver'
    r = await api_request(app, 'users', 'nosuchuser', 'server/progress')
    assert r.status_code == 404
    app_user = add_user(db, app=app, name=name)
    r = await api_request(app, 'users', name, 'server/progress')
    assert r.status_code == 404


async def test_progress_ready(request, app):
    """Test progress API when spawner is already started

    e.g. a race between requesting progress and progress already being complete
    """
    db = app.db
    name = 'saga'
    app_user = add_user(db, app=app, name=name)
    r = await api_request(app, 'users', name, 'server', method='post')
    r.raise_for_status()
    r = await api_request(app, 'users', name, 'server/progress', stream=True)
    r.raise_for_status()
    request.addfinalizer(r.close)
    assert r.headers['content-type'] == 'text/event-stream'
    ex = async_requests.executor
    line_iter = iter(r.iter_lines(decode_unicode=True))
    evt = await ex.submit(next_event, line_iter)
    assert evt['progress'] == 100
    assert evt['ready']
    assert evt['url'] == app_user.url


async def test_progress_ready_hook_async_func(request, app):
    """Test progress ready hook in Spawner class with an async function"""
    db = app.db
    name = 'saga'
    app_user = add_user(db, app=app, name=name)
    html_message = 'customized html message'
    spawner = app_user.spawner

    async def custom_progress_ready_hook(spawner, ready_event):
        ready_event['html_message'] = html_message
        return ready_event

    spawner.progress_ready_hook = custom_progress_ready_hook
    r = await api_request(app, 'users', name, 'server', method='post')
    r.raise_for_status()
    r = await api_request(app, 'users', name, 'server/progress', stream=True)
    r.raise_for_status()
    request.addfinalizer(r.close)
    assert r.headers['content-type'] == 'text/event-stream'
    ex = async_requests.executor
    line_iter = iter(r.iter_lines(decode_unicode=True))
    evt = await ex.submit(next_event, line_iter)
    assert evt['progress'] == 100
    assert evt['ready']
    assert evt['url'] == app_user.url
    assert evt['html_message'] == html_message


async def test_progress_ready_hook_sync_func(request, app):
    """Test progress ready hook in Spawner class with a sync function"""
    db = app.db
    name = 'saga'
    app_user = add_user(db, app=app, name=name)
    html_message = 'customized html message'
    spawner = app_user.spawner

    def custom_progress_ready_hook(spawner, ready_event):
        ready_event['html_message'] = html_message
        return ready_event

    spawner.progress_ready_hook = custom_progress_ready_hook
    r = await api_request(app, 'users', name, 'server', method='post')
    r.raise_for_status()
    r = await api_request(app, 'users', name, 'server/progress', stream=True)
    r.raise_for_status()
    request.addfinalizer(r.close)
    assert r.headers['content-type'] == 'text/event-stream'
    ex = async_requests.executor
    line_iter = iter(r.iter_lines(decode_unicode=True))
    evt = await ex.submit(next_event, line_iter)
    assert evt['progress'] == 100
    assert evt['ready']
    assert evt['url'] == app_user.url
    assert evt['html_message'] == html_message


async def test_progress_ready_hook_async_func_exception(request, app):
    """Test progress ready hook in Spawner class with an exception in
    an async function
    """
    db = app.db
    name = 'saga'
    app_user = add_user(db, app=app, name=name)
    html_message = f'Server ready at <a href="{app_user.url}">{app_user.url}</a>'
    spawner = app_user.spawner

    async def custom_progress_ready_hook(spawner, ready_event):
        ready_event["html_message"] = "."
        raise Exception()

    spawner.progress_ready_hook = custom_progress_ready_hook
    r = await api_request(app, 'users', name, 'server', method='post')
    r.raise_for_status()
    r = await api_request(app, 'users', name, 'server/progress', stream=True)
    r.raise_for_status()
    request.addfinalizer(r.close)
    assert r.headers['content-type'] == 'text/event-stream'
    ex = async_requests.executor
    line_iter = iter(r.iter_lines(decode_unicode=True))
    evt = await ex.submit(next_event, line_iter)
    assert evt['progress'] == 100
    assert evt['ready']
    assert evt['url'] == app_user.url
    assert evt['html_message'] == html_message


async def test_progress_bad(request, app, bad_spawn):
    """Test progress API when spawner has already failed"""
    db = app.db
    name = 'simon'
    app_user = add_user(db, app=app, name=name)
    r = await api_request(app, 'users', name, 'server', method='post')
    assert r.status_code == 500
    r = await api_request(app, 'users', name, 'server/progress', stream=True)
    r.raise_for_status()
    request.addfinalizer(r.close)
    assert r.headers['content-type'] == 'text/event-stream'
    ex = async_requests.executor
    line_iter = iter(r.iter_lines(decode_unicode=True))
    evt = await ex.submit(next_event, line_iter)
    assert evt == {
        'progress': 100,
        'failed': True,
        'message': "Spawn failed: I don't work!",
    }


async def test_progress_bad_slow(request, app, no_patience, slow_bad_spawn):
    """Test progress API when spawner fails while watching"""
    db = app.db
    name = 'eugene'
    app_user = add_user(db, app=app, name=name)
    r = await api_request(app, 'users', name, 'server', method='post')
    assert r.status_code == 202
    r = await api_request(app, 'users', name, 'server/progress', stream=True)
    r.raise_for_status()
    request.addfinalizer(r.close)
    assert r.headers['content-type'] == 'text/event-stream'
    ex = async_requests.executor
    line_iter = iter(r.iter_lines(decode_unicode=True))
    evt = await ex.submit(next_event, line_iter)
    assert evt['progress'] == 0
    evt = await ex.submit(next_event, line_iter)
    assert evt['progress'] == 50
    evt = await ex.submit(next_event, line_iter)
    assert evt == {
        'progress': 100,
        'failed': True,
        'message': "Spawn failed: I don't work!",
    }


async def progress_forever():
    """progress function that yields messages forever"""
    for i in range(1, 10):
        yield {'progress': i, 'message': f'Stage {i}'}
        # wait a long time before the next event
        await asyncio.sleep(10)


async def test_spawn_progress_cutoff(request, app, no_patience, slow_spawn):
    """Progress events stop when Spawner finishes

    even if progress iterator is still going.
    """
    db = app.db
    name = 'geddy'
    app_user = add_user(db, app=app, name=name)
    app_user.spawner.progress = progress_forever
    app_user.spawner.delay = 1

    r = await api_request(app, 'users', name, 'server', method='post')
    r.raise_for_status()
    r = await api_request(app, 'users', name, 'server/progress', stream=True)
    r.raise_for_status()
    request.addfinalizer(r.close)
    ex = async_requests.executor
    line_iter = iter(r.iter_lines(decode_unicode=True))
    evt = await ex.submit(next_event, line_iter)
    assert evt['progress'] == 0
    evt = await ex.submit(next_event, line_iter)
    assert evt == {'progress': 1, 'message': 'Stage 1'}
    evt = await ex.submit(next_event, line_iter)
    assert evt['progress'] == 100


async def test_spawn_limit(app, no_patience, slow_spawn, request):
    db = app.db
    p = mock.patch.dict(app.tornado_settings, {'concurrent_spawn_limit': 2})
    p.start()
    request.addfinalizer(p.stop)

    # start two pending spawns
    names = ['ykka', 'hjarka']
    users = [add_user(db, app=app, name=name) for name in names]
    users[0].spawner._start_future = asyncio.Future()
    users[1].spawner._start_future = asyncio.Future()
    for name in names:
        await api_request(app, 'users', name, 'server', method='post')
    assert app.users.count_active_users()['pending'] == 2

    # ykka and hjarka's spawns are both pending. Essun should fail with 429
    name = 'essun'
    user = add_user(db, app=app, name=name)
    user.spawner._start_future = asyncio.Future()
    r = await api_request(app, 'users', name, 'server', method='post')
    assert r.status_code == 429

    # allow ykka to start
    users[0].spawner._start_future.set_result(None)
    # wait for ykka to finish
    while not users[0].running:
        await asyncio.sleep(0.1)

    assert app.users.count_active_users()['pending'] == 1
    r = await api_request(app, 'users', name, 'server', method='post')
    r.raise_for_status()
    assert app.users.count_active_users()['pending'] == 2
    users.append(user)
    # allow hjarka and essun to finish starting
    for user in users[1:]:
        user.spawner._start_future.set_result(None)
    while not all(u.running for u in users):
        await asyncio.sleep(0.1)

    # everybody's running, pending count should be back to 0
    assert app.users.count_active_users()['pending'] == 0
    for u in users:
        u.spawner.delay = 0
        r = await api_request(app, 'users', u.name, 'server', method='delete')
        r.raise_for_status()
    while any(u.spawner.active for u in users):
        await asyncio.sleep(0.1)


@mark.slow
async def test_active_server_limit(app, request):
    db = app.db
    p = mock.patch.dict(app.tornado_settings, {'active_server_limit': 2})
    p.start()
    request.addfinalizer(p.stop)

    # start two pending spawns
    names = ['ykka', 'hjarka']
    users = [add_user(db, app=app, name=name) for name in names]
    for name in names:
        r = await api_request(app, 'users', name, 'server', method='post')
        r.raise_for_status()
    counts = app.users.count_active_users()
    assert counts['active'] == 2
    assert counts['ready'] == 2
    assert counts['pending'] == 0

    # ykka and hjarka's servers are running. Essun should fail with 429
    name = 'essun'
    user = add_user(db, app=app, name=name)
    r = await api_request(app, 'users', name, 'server', method='post')
    assert r.status_code == 429
    counts = app.users.count_active_users()
    assert counts['active'] == 2
    assert counts['ready'] == 2
    assert counts['pending'] == 0

    # stop one server
    await api_request(app, 'users', names[0], 'server', method='delete')
    counts = app.users.count_active_users()
    assert counts['active'] == 1
    assert counts['ready'] == 1
    assert counts['pending'] == 0

    r = await api_request(app, 'users', name, 'server', method='post')
    r.raise_for_status()
    counts = app.users.count_active_users()
    assert counts['active'] == 2
    assert counts['ready'] == 2
    assert counts['pending'] == 0
    users.append(user)

    # everybody's running, pending count should be back to 0
    assert app.users.count_active_users()['pending'] == 0
    for u in users:
        if not u.spawner.active:
            continue
        r = await api_request(app, 'users', u.name, 'server', method='delete')
        r.raise_for_status()

    counts = app.users.count_active_users()
    assert counts['active'] == 0
    assert counts['ready'] == 0
    assert counts['pending'] == 0


@mark.slow
async def test_start_stop_race(app, no_patience, slow_spawn):
    user = add_user(app.db, app, name='panda')
    spawner = user.spawner
    # start the server
    r = await api_request(app, 'users', user.name, 'server', method='post')
    assert r.status_code == 202
    assert spawner.pending == 'spawn'
    spawn_future = spawner._spawn_future
    # additional spawns while spawning shouldn't trigger a new spawn
    with mock.patch.object(spawner, 'start') as m:
        r = await api_request(app, 'users', user.name, 'server', method='post')
    assert r.status_code == 202
    assert m.call_count == 0

    # stop while spawning is okay now
    spawner.delay = 3
    r = await api_request(app, 'users', user.name, 'server', method='delete')
    assert r.status_code == 202
    assert spawner.pending == 'stop'
    assert spawn_future.cancelled()
    assert spawner._spawn_future is None
    # make sure we get past deleting from the proxy
    await asyncio.sleep(1)
    # additional stops while stopping shouldn't trigger a new stop
    with mock.patch.object(spawner, 'stop') as m:
        r = await api_request(app, 'users', user.name, 'server', method='delete')
    assert r.status_code == 202
    assert m.call_count == 0
    # start while stopping is not allowed
    with mock.patch.object(spawner, 'start') as m:
        r = await api_request(app, 'users', user.name, 'server', method='post')
    assert r.status_code == 400

    while spawner.active:
        await asyncio.sleep(0.1)
    # start after stop is okay
    r = await api_request(app, 'users', user.name, 'server', method='post')
    assert r.status_code == 202


async def test_get_proxy(app):
    r = await api_request(app, 'proxy')
    r.raise_for_status()
    reply = r.json()
    assert list(reply.keys()) == [app.hub.routespec]


@mark.parametrize("offset", (0, 1))
async def test_get_proxy_pagination(app, offset):
    r = await api_request(
        app, f'proxy?offset={offset}', headers={"Accept": PAGINATION_MEDIA_TYPE}
    )
    r.raise_for_status()
    reply = r.json()
    assert set(reply) == {"items", "_pagination"}
    assert list(reply["items"].keys()) == [app.hub.routespec][offset:]


async def test_cookie(app):
    db = app.db
    name = 'patience'
    user = add_user(db, app=app, name=name)
    r = await api_request(app, 'users', name, 'server', method='post')
    assert r.status_code == 201
    assert 'pid' in user.orm_spawners[''].state
    app_user = app.users[name]

    cookies = await app.login_user(name)
    cookie_name = app.hub.cookie_name
    # cookie jar gives '"cookie-value"', we want 'cookie-value'
    cookie = cookies[cookie_name][1:-1]
    r = await api_request(app, 'authorizations/cookie', cookie_name, "nothintoseehere")
    assert r.status_code == 404

    r = await api_request(
        app, 'authorizations/cookie', cookie_name, quote(cookie, safe='')
    )
    r.raise_for_status()
    reply = r.json()
    assert reply['name'] == name

    # deprecated cookie in body:
    r = await api_request(app, 'authorizations/cookie', cookie_name, data=cookie)
    r.raise_for_status()
    reply = r.json()
    assert reply['name'] == name


def normalize_token(token):
    for key in ('created', 'last_activity'):
        token[key] = normalize_timestamp(token[key])
    return token


async def test_check_token(app):
    name = 'book'
    user = add_user(app.db, app=app, name=name)
    token = user.new_api_token()
    r = await api_request(app, 'authorizations/token', token)
    r.raise_for_status()
    user_model = r.json()
    assert user_model['name'] == name
    r = await api_request(app, 'authorizations/token', 'notauthorized')
    assert r.status_code == 404


@mark.parametrize("headers, status", [({}, 404), ({'Authorization': 'token bad'}, 404)])
async def test_get_new_token_deprecated(app, headers, status):
    # request a new token
    r = await api_request(
        app, 'authorizations', 'token', method='post', headers=headers
    )
    assert r.status_code == status


@mark.parametrize(
    "headers, status, note, expires_in",
    [
        ({}, 201, 'test note', None),
        ({}, 201, '', 100),
        ({'Authorization': 'token bad'}, 403, '', None),
    ],
)
async def test_get_new_token(app, headers, status, note, expires_in):
    options = {}
    if note:
        options['note'] = note
    if expires_in:
        options['expires_in'] = expires_in
    if options:
        body = json.dumps(options)
    else:
        body = ''
    # request a new token
    r = await api_request(
        app, 'users/admin/tokens', method='post', headers=headers, data=body
    )
    assert r.status_code == status
    if status != 201:
        return
    # check the new-token reply
    reply = r.json()
    assert 'token' in reply
    assert reply['user'] == 'admin'
    assert reply['created']
    assert 'last_activity' in reply
    if expires_in:
        assert isinstance(reply['expires_at'], str)
    else:
        assert reply['expires_at'] is None
    if note:
        assert reply['note'] == note
    else:
        assert reply['note'] == 'Requested via api'
    token_id = reply['id']
    initial = normalize_token(reply)
    # pop token for later comparison
    initial.pop('token')

    # check the validity of the new token
    r = await api_request(app, 'users/admin/tokens', token_id)
    r.raise_for_status()
    reply = r.json()
    assert normalize_token(reply) == initial

    # delete the token
    r = await api_request(app, 'users/admin/tokens', token_id, method='delete')
    assert r.status_code == 204
    # verify deletion
    r = await api_request(app, 'users/admin/tokens', token_id)
    assert r.status_code == 404


@pytest.mark.parametrize(
    "expires_in_max, expires_in, expected",
    [
        (86400, None, 86400),
        (86400, 86400, 86400),
        (86400, 86401, 'error'),
        (3600, 100, 100),
        (None, None, None),
        (None, 86400, 86400),
    ],
)
async def test_token_expires_in_max(app, user, expires_in_max, expires_in, expected):
    options = {
        "expires_in": expires_in,
    }
    # request a new token
    with mock.patch.dict(
        app.tornado_settings, {"token_expires_in_max_seconds": expires_in_max}
    ):
        r = await api_request(
            app,
            f'users/{user.name}/tokens',
            method='post',
            data=json.dumps(options),
        )
    if expected == 'error':
        assert r.status_code == 400
        assert f"must not exceed {expires_in_max}" in r.json()["message"]
        return
    else:
        assert r.status_code == 201
    token_model = r.json()
    if expected is None:
        assert token_model["expires_at"] is None
    else:
        expected_expires_at = utcnow() + timedelta(seconds=expected)
        expires_at = parse_date(token_model["expires_at"])
        assert abs((expires_at - expected_expires_at).total_seconds()) < 30


@mark.parametrize(
    "as_user, for_user, status",
    [
        ('admin', 'other', 201),
        ('admin', 'missing', 403),
        ('user', 'other', 403),
        ('user', 'user', 201),
    ],
)
async def test_token_for_user(app, as_user, for_user, status):
    # ensure both users exist
    u = add_user(app.db, app, name=as_user)
    if for_user != 'missing':
        for_user_obj = add_user(app.db, app, name=for_user)
    data = {'username': for_user}
    headers = {'Authorization': f'token {u.new_api_token()}'}
    r = await api_request(
        app,
        'users',
        for_user,
        'tokens',
        method='post',
        data=json.dumps(data),
        headers=headers,
    )
    assert r.status_code == status
    reply = r.json()
    if status != 201:
        return
    assert 'token' in reply

    token_id = reply['id']
    r = await api_request(app, 'users', for_user, 'tokens', token_id, headers=headers)
    r.raise_for_status()
    reply = r.json()
    assert reply['user'] == for_user
    if for_user == as_user:
        note = 'Requested via api'
    else:
        note = f'Requested via api by user {as_user}'
    assert reply['note'] == note

    # delete the token
    r = await api_request(
        app, 'users', for_user, 'tokens', token_id, method='delete', headers=headers
    )

    assert r.status_code == 204
    r = await api_request(app, 'users', for_user, 'tokens', token_id, headers=headers)
    assert r.status_code == 404


async def test_token_authenticator_noauth(app):
    """Create a token for a user relying on Authenticator.authenticate and no auth header"""
    name = 'user'
    data = {'auth': {'username': name, 'password': name}}
    r = await api_request(
        app,
        'users',
        name,
        'tokens',
        method='post',
        data=json.dumps(data) if data else None,
        noauth=True,
    )
    assert r.status_code == 201
    reply = r.json()
    assert 'token' in reply
    r = await api_request(app, 'authorizations', 'token', reply['token'])
    r.raise_for_status()
    reply = r.json()
    assert reply['name'] == name


async def test_token_authenticator_dict_noauth(app):
    """Create a token for a user relying on Authenticator.authenticate and no auth header"""
    app.authenticator.auth_state = {'who': 'cares'}
    name = 'user'
    data = {'auth': {'username': name, 'password': name}}
    r = await api_request(
        app,
        'users',
        name,
        'tokens',
        method='post',
        data=json.dumps(data) if data else None,
        noauth=True,
    )
    assert r.status_code == 201
    reply = r.json()
    assert 'token' in reply
    r = await api_request(app, 'authorizations', 'token', reply['token'])
    r.raise_for_status()
    reply = r.json()
    assert reply['name'] == name


@mark.parametrize(
    "as_user, for_user, status",
    [
        ('admin', 'other', 200),
        ('admin', 'missing', 404),
        ('user', 'other', 404),
        ('user', 'user', 200),
    ],
)
async def test_token_list(app, as_user, for_user, status):
    u = add_user(app.db, app, name=as_user)
    if for_user != 'missing':
        for_user_obj = add_user(app.db, app, name=for_user)
    headers = {'Authorization': f'token {u.new_api_token()}'}
    r = await api_request(app, 'users', for_user, 'tokens', headers=headers)
    assert r.status_code == status
    if status != 200:
        return
    reply = r.json()
    assert sorted(reply) == ['api_tokens']
    assert len(reply['api_tokens']) == len(for_user_obj.api_tokens)
    assert all(token['user'] == for_user for token in reply['api_tokens'])
    # validate individual token ids
    for token in reply['api_tokens']:
        r = await api_request(
            app, 'users', for_user, 'tokens', token['id'], headers=headers
        )
        r.raise_for_status()
        reply = r.json()
        assert normalize_token(reply) == normalize_token(token)


# ---------------
# Group API tests
# ---------------


@mark.group
async def test_groups_list(app):
    r = await api_request(app, 'groups')
    r.raise_for_status()
    reply = r.json()
    assert reply == []

    # create two groups
    group = orm.Group(name='alphaflight')
    group_2 = orm.Group(name='betaflight')
    app.db.add(group)
    app.db.add(group_2)
    app.db.commit()

    r = await api_request(app, 'groups')
    r.raise_for_status()
    reply = r.json()
    assert reply == [
        {
            'kind': 'group',
            'name': 'alphaflight',
            'users': [],
            'roles': [],
            'properties': {},
        },
        {
            'kind': 'group',
            'name': 'betaflight',
            'users': [],
            'roles': [],
            'properties': {},
        },
    ]

    # Test offset for pagination
    r = await api_request(app, "groups?offset=1")
    r.raise_for_status()
    reply = r.json()
    assert r.status_code == 200
    assert reply == [
        {
            'kind': 'group',
            'name': 'betaflight',
            'users': [],
            'roles': [],
            'properties': {},
        }
    ]

    r = await api_request(app, "groups?offset=10")
    r.raise_for_status()
    reply = r.json()
    assert reply == []

    # Test limit for pagination
    r = await api_request(app, "groups?limit=1")
    r.raise_for_status()
    reply = r.json()
    assert r.status_code == 200
    assert reply == [
        {
            'kind': 'group',
            'name': 'alphaflight',
            'users': [],
            'roles': [],
            'properties': {},
        }
    ]

    # 0 is rounded up to 1
    r = await api_request(app, "groups?limit=0")
    r.raise_for_status()
    reply = r.json()
    assert reply == [
        {
            'kind': 'group',
            'name': 'alphaflight',
            'users': [],
            'roles': [],
            'properties': {},
        }
    ]


@mark.group
async def test_add_multi_group(app):
    db = app.db
    names = ['group1', 'group2']
    r = await api_request(
        app, 'groups', method='post', data=json.dumps({'groups': names})
    )
    assert r.status_code == 201
    reply = r.json()
    r_names = [group['name'] for group in reply]
    assert names == r_names

    # try to create the same groups again
    r = await api_request(
        app, 'groups', method='post', data=json.dumps({'groups': names})
    )
    assert r.status_code == 409


@mark.group
async def test_group_get(app):
    group = orm.Group(name='alphaflight')
    app.db.add(group)
    app.db.commit()
    group = orm.Group.find(app.db, name='alphaflight')
    user = add_user(app.db, app=app, name='sasquatch')
    group.users.append(user.orm_user)
    app.db.commit()

    r = await api_request(app, 'groups/runaways')
    assert r.status_code == 404

    r = await api_request(app, 'groups/alphaflight')
    r.raise_for_status()
    reply = r.json()
    assert reply == {
        'kind': 'group',
        'name': 'alphaflight',
        'users': ['sasquatch'],
        'roles': [],
        'properties': {},
    }


@mark.group
async def test_group_create_delete(app):
    db = app.db
    user = add_user(app.db, app=app, name='sasquatch')
    r = await api_request(app, 'groups/runaways', method='delete')
    assert r.status_code == 404

    r = await api_request(
        app, 'groups/new', method='post', data=json.dumps({'users': ['doesntexist']})
    )
    assert r.status_code == 400
    assert orm.Group.find(db, name='new') is None

    r = await api_request(
        app,
        'groups/omegaflight',
        method='post',
        data=json.dumps({'users': ['sasquatch']}),
    )
    r.raise_for_status()

    omegaflight = orm.Group.find(db, name='omegaflight')
    sasquatch = find_user(db, name='sasquatch')
    assert omegaflight in sasquatch.groups
    assert sasquatch in omegaflight.users

    # create duplicate raises 400
    r = await api_request(app, 'groups/omegaflight', method='post')
    assert r.status_code == 409

    r = await api_request(app, 'groups/omegaflight', method='delete')
    assert r.status_code == 204
    assert omegaflight not in sasquatch.groups
    assert orm.Group.find(db, name='omegaflight') is None

    # delete nonexistent gives 404
    r = await api_request(app, 'groups/omegaflight', method='delete')
    assert r.status_code == 404


@mark.group
async def test_group_add_delete_users(app):
    db = app.db
    group = orm.Group(name='alphaflight')
    app.db.add(group)
    app.db.commit()
    # must specify users
    r = await api_request(app, 'groups/alphaflight/users', method='post', data='{}')
    assert r.status_code == 400

    names = ['aurora', 'guardian', 'northstar', 'sasquatch', 'shaman', 'snowbird']
    users = [add_user(db, app=app, name=name) for name in names]
    r = await api_request(
        app,
        'groups/alphaflight/users',
        method='post',
        data=json.dumps({'users': names}),
    )
    r.raise_for_status()

    for user in users:
        print(user.name)
        assert [g.name for g in user.groups] == ['alphaflight']

    group = orm.Group.find(db, name='alphaflight')
    assert sorted(u.name for u in group.users) == sorted(names)

    r = await api_request(
        app,
        'groups/alphaflight/users',
        method='delete',
        data=json.dumps({'users': names[:2]}),
    )
    r.raise_for_status()

    for user in users[:2]:
        assert user.groups == []
    for user in users[2:]:
        assert [g.name for g in user.groups] == ['alphaflight']

    group = orm.Group.find(db, name='alphaflight')
    assert sorted(u.name for u in group.users) == sorted(names[2:])


@mark.parametrize(
    "properties",
    [
        "",
        "str",
        5,
        ["list"],
    ],
)
@mark.group
async def test_group_properties_invalid(app, group, properties):
    if properties:
        json_properties = json.dumps(properties)
    else:
        json_properties = ""
    have_properties = {"a": 5}
    group.properties = have_properties
    app.db.commit()
    r = await api_request(
        app, f"groups/{group.name}/properties", method='put', data=json_properties
    )
    assert r.status_code == 400
    # invalid requests didn't change properties
    assert group.properties == have_properties


@mark.group
async def test_group_properties(app, group):
    db = app.db
    # must specify properties
    properties = {
        "str": "x",
        "int": 5,
        "list": ["a"],
    }
    r = await api_request(
        app,
        f"groups/{group.name}/properties",
        method='put',
        data=json.dumps(properties),
    )
    r.raise_for_status()
    assert group.properties == properties

    r = await api_request(
        app,
        f"groups/{group.name}/properties",
        method='put',
        data="{}",
    )
    r.raise_for_status()
    assert group.properties == {}


@mark.group
async def test_auth_managed_groups(request, app, group, user):
    group.users.append(user.orm_user)
    app.db.commit()
    app.authenticator.manage_groups = True
    request.addfinalizer(lambda: setattr(app.authenticator, "manage_groups", False))
    # create groups
    r = await api_request(
        app,
        'groups',
        method='post',
        data=json.dumps({"groups": {"groupname": [user.name]}}),
    )
    assert r.status_code == 201
    r = await api_request(app, 'groups/newgroup', method='post')
    assert r.status_code == 201
    # add users to group
    r = await api_request(
        app,
        f'groups/{group.name}/users',
        method='post',
        data=json.dumps({"users": [user.name]}),
    )
    assert r.status_code == 200
    # remove users from group
    r = await api_request(
        app,
        f'groups/{group.name}/users',
        method='delete',
        data=json.dumps({"users": [user.name]}),
    )
    assert r.status_code == 200
    # delete groups
    r = await api_request(app, f'groups/{group.name}', method='delete')
    assert r.status_code == 204


# -----------------
# Service API tests
# -----------------


@mark.services
async def test_get_services(app, mockservice_url):
    mockservice = mockservice_url
    db = app.db
    r = await api_request(app, 'services')
    r.raise_for_status()
    assert r.status_code == 200

    services = r.json()
    assert services == {
        mockservice.name: {
            'kind': 'service',
            'name': mockservice.name,
            'admin': True,
            'roles': ['admin'],
            'command': mockservice.command,
            'pid': mockservice.proc.pid,
            'prefix': mockservice.server.base_url,
            'url': mockservice.url,
            'info': {},
            'display': True,
        }
    }
    r = await api_request(app, 'services', headers=auth_header(db, 'user'))
    assert r.status_code == 403


@mark.services
async def test_get_service(app, mockservice_url):
    mockservice = mockservice_url
    db = app.db
    r = await api_request(app, f"services/{mockservice.name}")
    r.raise_for_status()
    assert r.status_code == 200

    service = r.json()
    assert service == {
        'kind': 'service',
        'name': mockservice.name,
        'admin': True,
        'roles': ['admin'],
        'command': mockservice.command,
        'pid': mockservice.proc.pid,
        'prefix': mockservice.server.base_url,
        'url': mockservice.url,
        'info': {},
        'display': True,
    }
    r = await api_request(
        app,
        f"services/{mockservice.name}",
        headers={'Authorization': f'token {mockservice.api_token}'},
    )
    r.raise_for_status()

    r = await api_request(
        app, f"services/{mockservice.name}", headers=auth_header(db, 'user')
    )
    assert r.status_code == 403

    r = await api_request(app, "services/nosuchservice")
    assert r.status_code == 404


@pytest.fixture
def service_admin_user(create_user_with_scopes):
    return create_user_with_scopes('admin:services')


@mark.services
async def test_create_service(app, service_admin_user, service_name, service_data):
    db = app.db
    r = await api_request(
        app,
        f'services/{service_name}',
        headers=auth_header(db, service_admin_user.name),
        data=json.dumps(service_data),
        method='post',
    )

    r.raise_for_status()
    assert r.status_code == 201
    assert r.json()['name'] == service_name
    orm_service = orm.Service.find(db, service_name)
    assert orm_service is not None

    oath_client = (
        db.query(orm.OAuthClient)
        .filter_by(identifier=service_data['oauth_client_id'])
        .first()
    )
    assert oath_client.redirect_uri == service_data['oauth_redirect_uri']

    assert service_name in app._service_map
    assert (
        app._service_map[service_name].oauth_no_confirm
        == service_data['oauth_no_confirm']
    )


@mark.services
async def test_create_service_no_role(app, service_name, service_data):
    db = app.db
    r = await api_request(
        app,
        f'services/{service_name}',
        headers=auth_header(db, 'user'),
        data=json.dumps(service_data),
        method='post',
    )

    assert r.status_code == 403


@mark.services
async def test_create_service_conflict(
    app, service_admin_user, mockservice, service_data, service_name
):
    db = app.db
    app.services = [{'name': service_name}]
    app.init_services()

    r = await api_request(
        app,
        f'services/{service_name}',
        headers=auth_header(db, service_admin_user.name),
        data=json.dumps(service_data),
        method='post',
    )

    assert r.status_code == 409


@mark.services
async def test_create_service_duplication(
    app, service_admin_user, service_name, service_data
):
    db = app.db

    r = await api_request(
        app,
        f'services/{service_name}',
        headers=auth_header(db, service_admin_user.name),
        data=json.dumps(service_data),
        method='post',
    )
    assert r.status_code == 201

    r = await api_request(
        app,
        f'services/{service_name}',
        headers=auth_header(db, service_admin_user.name),
        data=json.dumps(service_data),
        method='post',
    )
    assert r.status_code == 409


@mark.services
async def test_create_managed_service(
    app, service_admin_user, service_name, service_data
):
    db = app.db
    managed_service_data = deepcopy(service_data)
    managed_service_data['command'] = ['foo']
    r = await api_request(
        app,
        f'services/{service_name}',
        headers=auth_header(db, service_admin_user.name),
        data=json.dumps(managed_service_data),
        method='post',
    )

    assert r.status_code == 400
    assert 'Can not create managed service' in r.json()['message']
    orm_service = orm.Service.find(db, service_name)
    assert orm_service is None


@mark.services
async def test_create_admin_service(app, admin_user, service_name, service_data):
    db = app.db
    managed_service_data = deepcopy(service_data)
    managed_service_data['admin'] = True
    r = await api_request(
        app,
        f'services/{service_name}',
        headers=auth_header(db, admin_user.name),
        data=json.dumps(managed_service_data),
        method='post',
    )

    assert r.status_code == 201
    orm_service = orm.Service.find(db, service_name)
    assert orm_service is not None


@mark.services
async def test_create_admin_service_without_admin_right(
    app, service_admin_user, service_data, service_name
):
    db = app.db
    managed_service_data = deepcopy(service_data)
    managed_service_data['admin'] = True
    r = await api_request(
        app,
        f'services/{service_name}',
        headers=auth_header(db, service_admin_user.name),
        data=json.dumps(managed_service_data),
        method='post',
    )

    assert r.status_code == 400
    assert 'Not assigning requested scopes' in r.json()['message']
    orm_service = orm.Service.find(db, service_name)
    assert orm_service is None


@mark.services
async def test_create_service_with_scope(
    app, create_user_with_scopes, service_name, service_data
):
    db = app.db
    managed_service_data = deepcopy(service_data)
    managed_service_data['oauth_client_allowed_scopes'] = ["admin:users"]
    managed_service_data['oauth_client_id'] = "service-client-with-scope"
    user_with_scope = create_user_with_scopes('admin:services', 'admin:users')
    r = await api_request(
        app,
        f'services/{service_name}',
        headers=auth_header(db, user_with_scope.name),
        data=json.dumps(managed_service_data),
        method='post',
    )

    assert r.status_code == 201
    orm_service = orm.Service.find(db, service_name)
    assert orm_service is not None


@mark.services
async def test_create_service_without_requested_scope(
    app,
    service_admin_user,
    service_data,
    service_name,
):
    db = app.db
    managed_service_data = deepcopy(service_data)
    managed_service_data['oauth_client_allowed_scopes'] = ["admin:users"]
    r = await api_request(
        app,
        f'services/{service_name}',
        headers=auth_header(db, service_admin_user.name),
        data=json.dumps(managed_service_data),
        method='post',
    )

    assert r.status_code == 400
    assert 'Not assigning requested scopes' in r.json()['message']
    orm_service = orm.Service.find(db, service_name)
    assert orm_service is None


@mark.services
async def test_delete_service(app, service_admin_user, service_name, service_data):
    db = app.db
    r = await api_request(
        app,
        f'services/{service_name}',
        headers=auth_header(db, service_admin_user.name),
        data=json.dumps(service_data),
        method='post',
    )
    assert r.status_code == 201

    r = await api_request(
        app,
        f'services/{service_name}',
        headers=auth_header(db, service_admin_user.name),
        method='delete',
    )
    assert r.status_code == 200

    orm_service = orm.Service.find(db, service_name)
    assert orm_service is None

    oath_client = (
        db.query(orm.OAuthClient)
        .filter_by(identifier=service_data['oauth_client_id'])
        .first()
    )
    assert oath_client is None

    assert service_name not in app._service_map

    r = await api_request(app, f"services/{service_name}", method="delete")
    assert r.status_code == 404


@mark.services
async def test_delete_service_from_config(app, service_admin_user, mockservice):
    db = app.db
    service_name = mockservice.name
    r = await api_request(
        app,
        f'services/{service_name}',
        headers=auth_header(db, service_admin_user.name),
        method='delete',
    )
    assert r.status_code == 405
    assert r.json()['message'] == f'Service {service_name} is not modifiable at runtime'


async def test_root_api(app):
    kwargs = {}
    if app.internal_ssl:
        kwargs['cert'] = (app.internal_ssl_cert, app.internal_ssl_key)
        kwargs["verify"] = app.internal_ssl_ca
    r = await api_request(app, bypass_proxy=True)
    r.raise_for_status()
    expected = {'version': jupyterhub.__version__}
    assert r.json() == expected


async def test_info(app):
    r = await api_request(app, 'info')
    r.raise_for_status()
    data = r.json()
    assert data['version'] == jupyterhub.__version__
    assert sorted(data) == [
        'authenticator',
        'python',
        'spawner',
        'sys_executable',
        'version',
    ]
    assert data['python'] == sys.version
    assert data['sys_executable'] == sys.executable
    assert data['authenticator'] == {
        'class': 'jupyterhub.tests.mocking.MockPAMAuthenticator',
        'version': jupyterhub.__version__,
    }
    assert data['spawner'] == {
        'class': 'jupyterhub.tests.mocking.MockSpawner',
        'version': jupyterhub.__version__,
    }


# ------------------
# Activity API tests
# ------------------


async def test_update_activity_403(app, user, admin_user):
    token = user.new_api_token()
    r = await api_request(
        app,
        f"users/{admin_user.name}/activity",
        headers={"Authorization": f"token {token}"},
        data="{}",
        method="post",
    )
    assert r.status_code == 404


async def test_update_activity_admin(app, user, admin_user):
    token = admin_user.new_api_token(roles=['admin'])
    r = await api_request(
        app,
        f"users/{user.name}/activity",
        headers={"Authorization": f"token {token}"},
        data=json.dumps({"last_activity": utcnow().isoformat()}),
        method="post",
    )
    r.raise_for_status()


@mark.parametrize(
    "server_name, fresh",
    [
        ("", True),
        ("", False),
        ("exists", True),
        ("exists", False),
        ("nope", True),
        ("nope", False),
    ],
)
async def test_update_server_activity(app, user, server_name, fresh):
    token = user.new_api_token()
    now = utcnow()
    internal_now = now.replace(tzinfo=None)
    # we use naive utc internally
    # initialize last_activity for one named and the default server
    for name in ("", "exists"):
        user.spawners[name].orm_spawner.last_activity = internal_now
    app.db.commit()

    td = timedelta(minutes=1)
    if fresh:
        activity = now + td
    else:
        activity = now - td

    r = await api_request(
        app,
        f"users/{user.name}/activity",
        headers={"Authorization": f"token {token}"},
        data=json.dumps(
            {"servers": {server_name: {"last_activity": activity.isoformat()}}}
        ),
        method="post",
    )
    if server_name == "nope":
        assert r.status_code == 400
        reply = r.json()
        assert server_name in reply["message"]
        assert "No such server" in reply["message"]
        assert user.name in reply["message"]
        return

    r.raise_for_status()

    # check that last activity was updated

    if fresh:
        expected = activity.replace(tzinfo=None)
    else:
        expected = now.replace(tzinfo=None)

    assert user.spawners[server_name].orm_spawner.last_activity == expected


# -----------------
# General API tests
# -----------------


async def test_options(app):
    r = await api_request(app, 'users', method='options')
    r.raise_for_status()
    assert 'Access-Control-Allow-Headers' in r.headers


async def test_bad_json_body(app):
    r = await api_request(app, 'users', method='post', data='notjson')
    assert r.status_code == 400


# ---------------------------------
# Shutdown MUST always be last test
# ---------------------------------


def test_shutdown(app):
    loop = app.io_loop

    # have to do things a little funky since we are going to stop the loop,
    # which makes gen_test unhappy. So we run the loop ourselves.

    async def shutdown():
        r = await api_request(
            app,
            'shutdown',
            method='post',
            data=json.dumps({'servers': True, 'proxy': True}),
        )
        return r

    real_stop = loop.asyncio_loop.stop

    def stop():
        stop.called = True
        loop.call_later(2, real_stop)

    real_cleanup = app.cleanup

    def cleanup():
        cleanup.called = True
        loop.call_later(1, real_cleanup)

    app.cleanup = cleanup

    with mock.patch.object(loop.asyncio_loop, 'stop', stop):
        r = loop.run_sync(shutdown, timeout=5)
    r.raise_for_status()
    reply = r.json()
    assert cleanup.called
    assert stop.called
