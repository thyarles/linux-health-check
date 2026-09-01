"""Kubernetes section.

The two facts these tests exist to protect:

  1. Discovery must work under CRON's environment, not the operator's. The rke2
     control planes get kubectl from an interactive `export PATH=...` that the
     nightly run never sees.
  2. A Kubernetes node is a noise machine. Completed helm pods, month-old
     Evicted objects, PVCs that are Pending by design and pods that were
     restarting when the cron fired must never reach anyone's inbox.
"""

import pytest

from hc.checks import (
    CAUTION, INFO, OK, UNHEALTHY,
    _k8s_age_seconds, _parse_pod_line, check_kubernetes,
)
from tests.conftest import alert_messages


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

NODES_CMD  = "get nodes -o jsonpath"
PODS_CMD   = "get pods --all-namespaces"
PVC_CMD    = "get pvc --all-namespaces"
EVENTS_CMD = "get events --all-namespaces"

ONE_NODE = ("k3s-01||v1.30.5+k3s1|"
            "MemoryPressure=False,DiskPressure=False,PIDPressure=False,Ready=True,|"
            "k3s-01,10.0.0.5,")


def _status_of(section, label):
    for row in section.rows:
        if row.label == label:
            return row.status
    raise AssertionError(f"no row labelled {label!r} in {[r.label for r in section.rows]}")


def _row_labels(section):
    return [r.label for r in section.rows]


def _found(shell, kubectl="/var/lib/rancher/rke2/bin/kubectl",
           kubeconfig="/etc/rancher/rke2/rke2.yaml"):
    """Wire up discovery: kubectl and kubeconfig both resolvable."""
    if kubectl:
        shell.expect("command -v", kubectl)
    if kubeconfig:
        shell.expect("for f in", kubeconfig)


def _cluster(shell, nodes=ONE_NODE, **kw):
    """A reachable single-node cluster with nothing else to report."""
    _found(shell, **kw)
    shell.expect(NODES_CMD, nodes)
    shell.expect("hostname -I", "10.0.0.5")


# ─────────────────────────────────────────────────────────────────────────────
# Discovery — the cron environment is the one that matters
# ─────────────────────────────────────────────────────────────────────────────

def test_kubectl_is_found_by_absolute_path_when_not_on_path(cfg, shell, tools, no_state):
    """The rke2 case: PATH=/usr/bin:/bin under cron, kubectl in /var/lib/rancher.

    `tools` reports nothing installed, which is exactly what shutil.which()
    returns during the nightly run.
    """
    _cluster(shell)
    section = check_kubernetes(cfg)
    assert section.applicable
    assert "/var/lib/rancher/rke2/bin/kubectl" in " ".join(shell.calls)
    assert _status_of(section, "k3s-01 (this host)") == OK


def test_no_kubectl_and_no_kubeconfig_is_silent(cfg, shell, tools, no_state):
    """A plain web server must not be told about Kubernetes, ever."""
    section = check_kubernetes(cfg)
    assert section.applicable is False
    assert alert_messages(section) == []
    assert section.missing_tools == []


def test_kubectl_without_a_kubeconfig_is_silent(cfg, shell, tools, no_state):
    """An agent node: kubectl exists, there is no cluster to ask. Permanent."""
    tools.add("kubectl")
    section = check_kubernetes(cfg)
    assert section.applicable is False
    assert alert_messages(section) == []


def test_scope_off_disables_the_section(cfg, shell, tools, no_state):
    cfg.set("kubernetes", "scope", "off")
    _cluster(shell)
    section = check_kubernetes(cfg)
    assert section.applicable is False


def test_unreachable_api_is_one_caution_not_unhealthy(cfg, shell, tools, no_state):
    _found(shell)
    shell.expect(NODES_CMD, "", rc=1,
                 err="The connection to the server 127.0.0.1:6443 was refused")
    section = check_kubernetes(cfg)
    assert section.status == CAUTION
    assert len(alert_messages(section)) == 1
    assert "unreachable" in alert_messages(section)[0]


def test_rejected_credentials_say_so(cfg, shell, tools, no_state):
    _found(shell)
    shell.expect(NODES_CMD, "error: You must be logged in to the server (Unauthorized)",
                 rc=1)
    section = check_kubernetes(cfg)
    assert "credentials" in alert_messages(section)[0]


# ─────────────────────────────────────────────────────────────────────────────
# Parsing — kubectl's output has moved twice
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("age,seconds", [
    ("45s",    45),
    ("5m",     300),
    ("5m30s",  330),
    ("3h20m",  3 * 3600 + 20 * 60),
    ("36h",    36 * 3600),
    ("5d3h",   5 * 86400 + 3 * 3600),
    ("2y40d",  2 * 31536000 + 40 * 86400),
])
def test_age_parsing(age, seconds):
    assert _k8s_age_seconds(age) == pytest.approx(seconds)


def test_unparseable_age_is_not_used_to_escalate():
    assert _k8s_age_seconds("<unknown>") == -1.0


@pytest.mark.parametrize("line,restarts,since", [
    # kubectl < 1.23: RESTARTS is a bare int, six tokens.
    ("default api-1 1/2 CrashLoopBackOff 12 3d4h", 12, ""),
    # kubectl >= 1.23: RESTARTS carries '(5m ago)', eight tokens.
    ("default api-1 1/2 CrashLoopBackOff 12 (5m ago) 3d4h", 12, "5m ago"),
    ("kube-system coredns-x 1/1 Running 0 5d", 0, ""),
])
def test_restarts_column_both_shapes(line, restarts, since):
    p = _parse_pod_line(line)
    assert p["restarts"] == restarts
    assert p["since"] == since
    assert p["age"] in ("3d4h", "5d")
    assert p["status"] == p["status"].strip()


def test_ready_fraction_is_split():
    p = _parse_pod_line("default api-1 1/2 Running 0 5d")
    assert (p["ready_n"], p["ready_of"]) == (1, 2)


# ─────────────────────────────────────────────────────────────────────────────
# Nodes
# ─────────────────────────────────────────────────────────────────────────────

def test_notready_on_this_host_is_unhealthy(cfg, shell, tools, no_state):
    _cluster(shell, nodes="k3s-01||v1.30.5|Ready=False,|k3s-01,10.0.0.5,")
    section = check_kubernetes(cfg)
    assert _status_of(section, "k3s-01 (this host)") == UNHEALTHY
    assert "NotReady" in alert_messages(section)[0]


def test_notready_elsewhere_is_only_caution(cfg, shell, tools, no_state):
    """Another node runs its own health check; do not page this host at full
    severity for someone else's machine."""
    _cluster(shell, nodes="\n".join([
        "k3s-01||v1.30.5|Ready=True,|k3s-01,10.0.0.5,",
        "k3s-02||v1.30.5|Ready=Unknown,|k3s-02,10.0.0.6,",
    ]))
    shell.expect("--field-selector=spec.nodeName=", "")
    section = check_kubernetes(cfg)
    assert _status_of(section, "k3s-02") == CAUTION
    assert section.status == CAUTION


def test_a_node_cordoned_on_both_runs_is_not_news(cfg, shell, tools, no_state):
    """Cordoning is deliberate maintenance. It must not be a permanent yellow."""
    no_state["k8s_cordoned"] = ["k3s-01"]
    _cluster(shell, nodes="k3s-01|true|v1.30.5|Ready=True,|k3s-01,10.0.0.5,")
    section = check_kubernetes(cfg)
    assert _status_of(section, "k3s-01 (this host)") == INFO
    assert alert_messages(section) == []


def test_a_newly_cordoned_node_is_reported_once(cfg, shell, tools, no_state):
    _cluster(shell, nodes="k3s-01|true|v1.30.5|Ready=True,|k3s-01,10.0.0.5,")
    section = check_kubernetes(cfg)
    assert _status_of(section, "k3s-01 (this host)") == CAUTION
    assert "cordoned" in alert_messages(section)[0]
    assert no_state["k8s_cordoned"] == ["k3s-01"]


def test_disk_pressure_is_caution_and_grouped(cfg, shell, tools, no_state):
    _cluster(shell, nodes="k3s-01||v1.30.5|DiskPressure=True,Ready=True,|k3s-01,10.0.0.5,")
    section = check_kubernetes(cfg)
    assert _status_of(section, "k3s-01 (this host)") == CAUTION
    assert len(alert_messages(section)) == 1
    assert "DiskPressure" in alert_messages(section)[0]


def test_pressure_unknown_on_a_dead_node_is_not_reported_four_times(cfg, shell, tools,
                                                                   no_state):
    """A kubelet that stopped reporting sets every condition to Unknown. That is
    one fact, not five."""
    _cluster(shell, nodes=("k3s-01||v1.30.5|MemoryPressure=Unknown,DiskPressure=Unknown,"
                           "PIDPressure=Unknown,Ready=Unknown,|k3s-01,10.0.0.5,"))
    section = check_kubernetes(cfg)
    assert len(alert_messages(section)) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Pods — this is where the noise lives
# ─────────────────────────────────────────────────────────────────────────────

def test_completed_helm_and_cronjob_pods_never_alert(cfg, shell, tools, no_state):
    """The single biggest Kubernetes noise source: install pods that finished
    successfully months ago and sit in the list forever."""
    _cluster(shell)
    shell.expect(PODS_CMD, "\n".join([
        "kube-system helm-install-traefik-2xk9p 0/1 Completed 0 13d",
        "kube-system helm-install-traefik-crd-p4b2z 0/1 Completed 0 13d",
        "default backup-cron-28912 0/1 Completed 0 6h",
        "kube-system coredns-6799fbcd5-abcde 1/1 Running 0 13d",
    ]))
    section = check_kubernetes(cfg)
    assert alert_messages(section) == []
    assert section.status != CAUTION


def test_month_old_evicted_pods_are_info(cfg, shell, tools, no_state):
    """Evicted pod objects persist until GC — last month's eviction is not
    today's news. Same reasoning as docker's 'Exited (255) 6 weeks ago'."""
    _cluster(shell)
    shell.expect(PODS_CMD, "default legacy-abc123 0/1 Evicted 0 27d")
    section = check_kubernetes(cfg)
    assert alert_messages(section) == []


def test_a_recent_eviction_still_alerts(cfg, shell, tools, no_state):
    _cluster(shell)
    shell.expect(PODS_CMD, "default legacy-abc123 0/1 Evicted 0 12m")
    section = check_kubernetes(cfg)
    assert len(alert_messages(section)) == 1


def test_a_pod_restarting_when_cron_fired_is_not_an_incident(cfg, shell, tools, no_state):
    """0/1 Running for a few seconds at 07:00 is a rolling restart, not an
    outage. Only a pod still degraded on the NEXT run is news."""
    _cluster(shell)
    shell.expect(PODS_CMD, "default api-7d8f9c4b5-qz2lm 0/1 Running 0 30s")
    section = check_kubernetes(cfg)
    assert alert_messages(section) == []
    assert no_state["k8s_degraded_pods"] == ["default/api-7d8f9c4b5-qz2lm"]


def test_a_pod_degraded_on_two_runs_does_alert(cfg, shell, tools, no_state):
    no_state["k8s_degraded_pods"] = ["default/api-7d8f9c4b5-qz2lm"]
    _cluster(shell)
    shell.expect(PODS_CMD, "default api-7d8f9c4b5-qz2lm 0/1 Running 0 2d")
    section = check_kubernetes(cfg)
    assert len(alert_messages(section)) == 1


def test_young_pending_is_the_scheduler_working(cfg, shell, tools, no_state):
    _cluster(shell)
    shell.expect(PODS_CMD, "monitoring prom-0 0/1 Pending 0 2m30s")
    assert alert_messages(check_kubernetes(cfg)) == []


def test_pending_past_the_threshold_alerts(cfg, shell, tools, no_state):
    _cluster(shell)
    shell.expect(PODS_CMD, "monitoring prom-0 0/1 Pending 0 3h")
    assert len(alert_messages(check_kubernetes(cfg))) == 1


def test_crashloop_alerts(cfg, shell, tools, no_state):
    _cluster(shell)
    shell.expect(PODS_CMD, "default api-1 1/2 CrashLoopBackOff 12 (5m ago) 3d4h")
    section = check_kubernetes(cfg)
    assert _status_of(section, "default/api-1") == CAUTION
    assert "default/api-1" in alert_messages(section)[0]


def test_a_high_but_unchanged_restart_count_is_not_news(cfg, shell, tools, no_state):
    """A pod up 200 days with 47 restarts is healthy. The count is meaningless;
    the growth since the last run is the signal."""
    no_state["k8s_pod_restarts"] = {"default/worker-1": 47}
    _cluster(shell)
    shell.expect(PODS_CMD, "default worker-1 1/1 Running 47 200d")
    section = check_kubernetes(cfg)
    assert alert_messages(section) == []


def test_restart_growth_since_the_last_run_does_alert(cfg, shell, tools, no_state):
    no_state["k8s_pod_restarts"] = {"default/worker-1": 43}
    _cluster(shell)
    shell.expect(PODS_CMD, "default worker-1 1/1 Running 47 200d")
    section = check_kubernetes(cfg)
    assert len(alert_messages(section)) == 1
    assert _status_of(section, "default/worker-1") == CAUTION


def test_a_pod_seen_for_the_first_time_has_no_growth(cfg, shell, tools, no_state):
    """A rolling deploy makes a new pod name. It has no history, so it cannot
    have grown — otherwise every deploy would alert."""
    _cluster(shell)
    shell.expect(PODS_CMD, "default worker-2 1/1 Running 9 5m")
    assert alert_messages(check_kubernetes(cfg)) == []


def test_healthy_pods_are_a_count_not_a_wall(cfg, shell, tools, no_state):
    _cluster(shell)
    shell.expect(PODS_CMD, "\n".join(
        f"ns{i} pod-{i} 1/1 Running 0 5d" for i in range(60)))
    section = check_kubernetes(cfg)
    assert not any(lbl.startswith("ns") for lbl in _row_labels(section))
    assert "60 running" in [r.value for r in section.rows if r.label.startswith("Pods")][0]


def test_problem_pods_are_capped(cfg, shell, tools, no_state):
    _cluster(shell)
    shell.expect(PODS_CMD, "\n".join(
        f"ns{i} bad-{i} 0/1 CrashLoopBackOff 3 5d" for i in range(25)))
    section = check_kubernetes(cfg)
    assert len([r for r in section.rows if r.label.startswith("ns")]) == 10
    assert any("15 more pod(s)" in r.value for r in section.rows)
    assert len(alert_messages(section)) == 1


def test_unknown_pod_status_never_invents_an_incident(cfg, shell, tools, no_state):
    _cluster(shell)
    shell.expect(PODS_CMD, "default weird-1 0/1 SomeFuturePhase 0 5d")
    assert alert_messages(check_kubernetes(cfg)) == []


def test_multi_node_cluster_scopes_pods_to_this_host(cfg, shell, tools, no_state):
    """auto scope: on a real cluster each node's report leads with its own pods,
    so the same CrashLoopBackOff is not emailed once per node."""
    shell.expect("--field-selector=spec.nodeName=", "default mine-1 1/1 Running 0 5d")
    _cluster(shell, nodes="\n".join([
        "k3s-01||v1.30.5|Ready=True,|k3s-01,10.0.0.5,",
        "k3s-02||v1.30.5|Ready=True,|k3s-02,10.0.0.6,",
    ]))
    section = check_kubernetes(cfg)
    assert shell.ran("--field-selector=spec.nodeName=k3s-01")
    assert any(r.label == "Pods (on this host)" for r in section.rows)


# ─────────────────────────────────────────────────────────────────────────────
# PVCs
# ─────────────────────────────────────────────────────────────────────────────

def test_a_pvc_pending_by_design_is_not_flagged_on_first_sight(cfg, shell, tools,
                                                               no_state):
    """local-path and most cloud classes are WaitForFirstConsumer, so a claim
    with no consumer is Pending forever ON PURPOSE."""
    _cluster(shell)
    shell.expect(PVC_CMD, "default tmp-cache Pending local-path")
    section = check_kubernetes(cfg)
    assert _status_of(section, "default/tmp-cache") == INFO
    assert alert_messages(section) == []


def test_a_pvc_pending_on_two_runs_alerts(cfg, shell, tools, no_state):
    no_state["k8s_pending_pvcs"] = ["default/tmp-cache"]
    _cluster(shell)
    shell.expect(PVC_CMD, "default tmp-cache Pending local-path")
    section = check_kubernetes(cfg)
    assert _status_of(section, "default/tmp-cache") == CAUTION
    assert len(alert_messages(section)) == 1


def test_a_lost_pvc_is_unhealthy_immediately(cfg, shell, tools, no_state):
    _cluster(shell)
    shell.expect(PVC_CMD, "default data-postgres-0 Lost local-path")
    section = check_kubernetes(cfg)
    assert section.status == UNHEALTHY


def test_bound_pvcs_are_a_count(cfg, shell, tools, no_state):
    _cluster(shell)
    shell.expect(PVC_CMD, "\n".join([
        "default data-postgres-0 Bound local-path",
        "monitoring prom-data Bound <none>",
    ]))
    section = check_kubernetes(cfg)
    assert _status_of(section, "PersistentVolumeClaims") == INFO
    assert alert_messages(section) == []


# ─────────────────────────────────────────────────────────────────────────────
# Events
# ─────────────────────────────────────────────────────────────────────────────

def test_routine_warning_events_are_a_count_at_any_volume(cfg, shell, tools, no_state):
    """A busy cluster emits hundreds of Unhealthy probe events an hour."""
    _cluster(shell)
    shell.expect(EVENTS_CMD, "\n".join([
        "Unhealthy 340 <none>",
        "BackOff 55 <none>",
        "FailedScheduling 12 <none>",
    ]))
    section = check_kubernetes(cfg)
    assert _status_of(section, "Warning Events") == INFO
    assert alert_messages(section) == []


def test_node_level_events_do_alert(cfg, shell, tools, no_state):
    _cluster(shell)
    shell.expect(EVENTS_CMD, "\n".join([
        "Unhealthy 11 <none>",
        "SystemOOM 2 <none>",
    ]))
    section = check_kubernetes(cfg)
    assert _status_of(section, "Node-level Events") == CAUTION
    assert "SystemOOM" in alert_messages(section)[0]


def test_the_event_window_is_never_called_24h(cfg, shell, tools, no_state):
    """--event-ttl defaults to ONE HOUR on k3s and rke2. Claiming a 24h window
    would be a lie that makes a cluster look quiet after an incident."""
    _cluster(shell)
    shell.expect(EVENTS_CMD, "Unhealthy 3 <none>")
    section = check_kubernetes(cfg)
    value = [r.value for r in section.rows if r.label == "Warning Events"][0]
    assert "24h" not in value
    assert "retained" in value


# ─────────────────────────────────────────────────────────────────────────────
# Images
# ─────────────────────────────────────────────────────────────────────────────

def test_images_are_a_count_by_default(cfg, shell, tools, no_state):
    _cluster(shell)
    shell.expect("for e in", "")           # no crictl socket probe result needed
    shell.expect("crictl", "")
    section = check_kubernetes(cfg)
    assert "Container Images" not in _row_labels(section)


def test_no_crictl_says_nothing_at_all(cfg, shell, tools, no_state):
    _cluster(shell)
    section = check_kubernetes(cfg)
    assert "Container Images" not in _row_labels(section)
    assert section.missing_tools == []


# ─────────────────────────────────────────────────────────────────────────────
# Per-signal flags
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("flag,cmd", [
    ("pods",   PODS_CMD),
    ("pvcs",   PVC_CMD),
    ("events", EVENTS_CMD),
])
def test_each_signal_can_be_switched_off(cfg, shell, tools, no_state, flag, cmd):
    cfg.set("kubernetes", flag, "false")
    _cluster(shell)
    check_kubernetes(cfg)
    assert not shell.ran(cmd)
