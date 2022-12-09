from unittest import mock

import pytest
from freezegun import freeze_time

from sentry.eventstore.models import Event
from sentry.ingest.transaction_clusterer import rules
from sentry.ingest.transaction_clusterer.base import ReplacementRule
from sentry.ingest.transaction_clusterer.datasource.redis import (
    _store_transaction_name,
    get_transaction_names,
    record_transaction_name,
)
from sentry.ingest.transaction_clusterer.tasks import cluster_projects, spawn_clusterers
from sentry.ingest.transaction_clusterer.tree import TreeClusterer
from sentry.models.project import Project
from sentry.testutils.helpers import Feature


def test_multi_fanout():
    clusterer = TreeClusterer(merge_threshold=3)
    transaction_names = [
        "/a/b0/c/d0/e",
        "/a/b0/c/d1/e",
        "/a/b0/c/d2/e",
        "/a/b1/c/d0/e",
        "/a/b1/c/d1/e/",
        "/a/b1/c/d2/e",
        "/a/b2/c/d0/e",
        "/a/b2/c/d1/e/",
        "/a/b2/c/d2/e",
        "/a/b2/c1/d2/e",
    ]
    clusterer.add_input(transaction_names)
    assert clusterer.get_rules() == ["/a/*/c/*/**", "/a/*/**"]


def test_single_leaf():
    clusterer = TreeClusterer(merge_threshold=2)
    transaction_names = [
        "/a/b1/c/",
        "/a/b2/c/",
    ]
    clusterer.add_input(transaction_names)
    assert clusterer.get_rules() == ["/a/*/**"]


@mock.patch("sentry.ingest.transaction_clusterer.datasource.redis.MAX_SET_SIZE", 5)
def test_collection():
    project1 = Project(id=101, name="p1", organization_id=1)
    project2 = Project(id=102, name="project2", organization_id=1)

    for project in (project1, project2):
        for i in range(len(project.name)):
            _store_transaction_name(project, f"tx-{project.name}-{i}")
            _store_transaction_name(project, f"tx-{project.name}-{i}")

    set_entries1 = set(get_transaction_names(project1))
    assert set_entries1 == {"tx-p1-0", "tx-p1-1"}

    set_entries2 = set(get_transaction_names(project2))
    assert len(set_entries2) == 5, set_entries2
    # We don't know which entries made it into the final set:
    for name in set_entries2:
        assert name.startswith("tx-project2-")

    project3 = Project(id=103, name="project3", organization_id=1)
    assert set() == set(get_transaction_names(project3))


@mock.patch("sentry.ingest.transaction_clusterer.datasource.redis.MAX_SET_SIZE", 100)
def test_distribution():
    """Make sure that the redis set prefers newer entries"""
    project = Project(id=103, name="", organization_id=1)
    for i in range(1000):
        _store_transaction_name(project, str(i))

    freshness = sum(map(int, get_transaction_names(project))) / 100

    # The average is usually around ~900, check for > 800 to be on the safe side
    assert freshness > 800, freshness


@mock.patch("sentry.ingest.transaction_clusterer.datasource.redis._store_transaction_name")
@pytest.mark.django_db
@pytest.mark.parametrize(
    "source,txname,feature_enabled,expected",
    [
        ("url", "/a/b/c", True, 1),
        ("route", "/", True, 0),
        ("url", None, True, 0),
        ("url", "/", False, 0),
        ("route", None, False, 0),
    ],
)
def test_record_transactions(
    mocked_record, default_organization, source, txname, feature_enabled, expected
):
    with Feature({"organizations:transaction-name-clusterer": feature_enabled}):
        project = Project(id=111, name="project", organization_id=default_organization.id)
        event = Event(
            project.id,
            "02552061b47b467cb38d1d2dd26eed21",
            data={
                "tags": [["transaction", txname]],
                "transaction": txname,
                "transaction_info": {"source": source},
            },
        )
        record_transaction_name(project, event)
        assert len(mocked_record.mock_calls) == expected


def test_save_rules():
    project = Project(id=111, name="project", organization_id=1)

    project_rules = rules.get_rules(project)
    assert project_rules == {}

    with freeze_time("2012-01-14 12:00:01"):
        rules.update_rules(project, [ReplacementRule("foo"), ReplacementRule("bar")])
    project_rules = rules.get_rules(project)
    assert project_rules == {"foo": "1334318401", "bar": "1334318401"}

    with freeze_time("2012-01-14 12:00:02"):
        rules.update_rules(project, [ReplacementRule("bar"), ReplacementRule("zap")])
    project_rules = rules.get_rules(project)
    assert {"bar": "1334318402", "foo": "1334318401", "zap": "1334318402"}


@mock.patch("django.conf.settings.SENTRY_TRANSACTION_CLUSTERER_RUN", True)
@mock.patch("sentry.ingest.transaction_clusterer.tasks.MERGE_THRESHOLD", 5)
@mock.patch(
    "sentry.ingest.transaction_clusterer.tasks.cluster_projects.delay",
    wraps=cluster_projects,  # call immediately
)
@pytest.mark.django_db
def test_run_clusterer_task(cluster_projects_delay, default_organization):
    with Feature({"organizations:transaction-name-clusterer": True}):
        project1 = Project(id=123, name="project1", organization_id=default_organization.id)
        project2 = Project(id=223, name="project2", organization_id=default_organization.id)
        for project in (project1, project2):
            project.save()
            for i in range(len(project.name)):
                _store_transaction_name(project, f"/user/tx-{project.name}-{i}")
                _store_transaction_name(project, f"/org/tx-{project.name}-{i}")

        spawn_clusterers()

        assert cluster_projects_delay.call_count == 1
        cluster_projects_delay.reset_mock()

        pr1_rules = rules.get_rules(project1)
        pr2_rules = rules.get_rules(project2)

        assert set(pr1_rules.keys()) == {"/org/*/**", "/user/*/**"}
        assert set(pr2_rules.keys()) == {"/org/*/**", "/user/*/**"}

        # add more transactions to the project 1
        for i in range(6):
            _store_transaction_name(project1, f"/users/trans/tx-{project1.id}-{i}")
            _store_transaction_name(project1, f"/test/path/{i}")

        with mock.patch("sentry.ingest.transaction_clusterer.tasks.PROJECTS_PER_TASK", 1):
            spawn_clusterers()

        # One project per batch now:
        assert cluster_projects_delay.call_count == 2

        pr_rules = rules.get_rules(project1)
        assert pr_rules.keys() == {
            "/org/*/**",
            "/user/*/**",
            "/test/path/*/**",
            "/users/trans/*/**",
        }
