from unittest import mock
from urllib.parse import urlparse

import pytest

from .. import orm
from ..user import UserDict
from .utils import add_user


@pytest.mark.parametrize("attr", ["self", "id", "name"])
async def test_userdict_get(db, attr):
    u = add_user(db, name="rey", app=False)
    userdict = UserDict(db_factory=lambda: db, settings={})

    if attr == "self":
        key = u
    else:
        key = getattr(u, attr)

    # `in` checks cache only
    assert key not in userdict
    assert userdict.get(key)
    assert userdict.get(key).id == u.id
    # `in` should find it now
    assert key in userdict


@pytest.mark.parametrize(
    "group_names",
    [
        ["isin1", "isin2"],
        ["isin1"],
        ["notin", "isin1"],
        ["new-group", "new-group", "isin1"],
        [],
    ],
)
def test_sync_groups(app, user, group_names):
    expected = sorted(set(group_names))
    db = app.db
    db.add(orm.Group(name="notin"))
    in_groups = [orm.Group(name="isin1"), orm.Group(name="isin2")]
    for group in in_groups:
        db.add(group)
    db.commit()
    user.groups = in_groups
    db.commit()
    user.sync_groups(group_names)
    assert not app.db.dirty
    after_groups = sorted(g.name for g in user.groups)
    assert after_groups == expected
    # double-check backref
    for group in db.query(orm.Group):
        if group.name in expected:
            assert user.orm_user in group.users
        else:
            assert user.orm_user not in group.users


@pytest.mark.parametrize(
    "server_name, path",
    [
        ("", ""),
        ("name", "name/"),
        ("næme", "n%C3%A6me/"),
    ],
)
def test_server_url(app, user, server_name, path):
    user_url = user.url
    assert user.server_url(server_name) == user_url + path


@pytest.mark.parametrize(
    "server_name, public_url, subdomain_host, expected_url",
    [
        ("", "", "", ""),
        ("name", "", "", ""),
        ("", "https://hub.tld/PREFIX/", "", "https://hub.tld/PREFIX/user/USERNAME/"),
        (
            "name",
            "https://hub.tld/PREFIX/",
            "",
            "https://hub.tld/PREFIX/user/USERNAME/name/",
        ),
        (
            "name",
            "",
            "https://hub.tld:123",
            "https://USERNAME.hub.tld:123/PREFIX/user/USERNAME/name/",
        ),
    ],
)
def test_public_url(app, user, server_name, public_url, subdomain_host, expected_url):
    expected_url = expected_url.replace("USERNAME", user.escaped_name).replace(
        "PREFIX", app.base_url.strip("/")
    )
    if public_url:
        public_url = public_url.replace("PREFIX", app.base_url.strip("/"))
        public_url = urlparse(public_url)
    with mock.patch.dict(
        user.settings,
        {
            "subdomain_host": subdomain_host,
            "domain": urlparse(subdomain_host).hostname,
            "public_url": public_url,
        },
    ):
        public_server_url = user.public_url(server_name)
    assert public_server_url == expected_url
