"""Render deployment combinations and check cross-resource contracts without a cluster."""
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / 'charts/moyai'


def render(example='bundled', release='test', *settings):
    result = subprocess.run(['helm', 'template', release, str(CHART), '-f', str(CHART / f'ci/{example}.yaml'),
                             *settings], capture_output=True, text=True, check=True)
    return [obj for obj in yaml.safe_load_all(result.stdout) if obj]


def named(objects, kind, name):
    return next(obj for obj in objects if obj['kind'] == kind and obj['metadata']['name'] == name)


def test_split_roles_share_only_the_private_state_and_public_marker():
    objects = render()
    pod = named(objects, 'Deployment', 'test-moyai')['spec']['template']['spec']
    containers = {container['name']: container for container in pod['containers']}
    assert set(containers) == {'api', 'coordinator', 'broker', 'worker', 'activation', 'proxy'}
    assert len({containers[role]['ports'][0]['containerPort'] for role in ('api','coordinator','broker','worker')}) == 4
    assert all('moyai.kubernetes_runtime' in containers[role]['command'] for role in ('api','coordinator','broker','worker','activation'))
    assert 'state' not in {volume['name'] for volume in containers['proxy']['volumeMounts']}
    assert pod['terminationGracePeriodSeconds'] >= 300
    assert named(objects, 'Service', 'test-moyai')['spec']['publishNotReadyAddresses'] is True
    config = named(objects, 'ConfigMap', 'test-moyai-config')['data']
    assert config['TEMPORAL_ADDRESS'] == 'test-moyai-temporal:7233'
    proxy = named(objects, 'ConfigMap', 'test-moyai-scripts')['data']['nginx.conf']
    assert 'location /moyai/hooks/' in proxy and 'location /moyai/broker/' in proxy
    assert 'location / { return 404; }' in proxy
    ingress = named(objects, 'Ingress', 'test-moyai')['spec']['rules'][0]
    assert ingress['host'] == 'gateway.example.com'
    assert ingress['http']['paths'][0]['path'] == '/moyai'
    assert ingress['http']['paths'][0]['backend']['service']['name'] == 'test-moyai'


def test_dependencies_and_state_can_be_supplied_externally():
    objects = render('external')
    assert not [obj for obj in objects if obj['kind'] in {'PersistentVolumeClaim', 'ServiceAccount', 'Ingress'}]
    assert [obj['metadata']['name'] for obj in objects if obj['kind'] == 'Deployment'] == ['test-moyai']
    pod = named(objects, 'Deployment', 'test-moyai')['spec']['template']['spec']
    assert [step['name'] for step in pod['initContainers']] == ['permissions', 'initialize']
    assert pod['serviceAccountName'] == 'existing-moyai'
    assert pod['volumes'][0]['persistentVolumeClaim']['claimName'] == 'retained-moyai-state'
    assert not any(obj['metadata']['name'].endswith('-temporal-dynamic') for obj in objects)


def test_bundled_temporal_has_a_readable_dynamic_config_file():
    objects = render()
    pod = named(objects, 'Deployment', 'test-moyai-temporal')['spec']['template']['spec']
    server = next(container for container in pod['containers'] if container['name'] == 'temporal')
    config_path = Path(next(env['value'] for env in server['env'] if env['name'] == 'DYNAMIC_CONFIG_FILE_PATH'))
    mount = next(mount for mount in server['volumeMounts'] if config_path.parent == Path(mount['mountPath']))
    assert mount['readOnly'] is True
    volume = next(volume for volume in pod['volumes'] if volume['name'] == mount['name'])
    config = named(objects, 'ConfigMap', volume['configMap']['name'])['data']
    assert yaml.safe_load(config[config_path.name]) == {}


def test_releases_do_not_alias_services_claims_or_selectors():
    a, b = render(release='one'), render(release='two')
    assert not ({(o['kind'],o['metadata']['name']) for o in a} & {(o['kind'],o['metadata']['name']) for o in b})
    for objects in (a, b):
        for claim in [o for o in objects if o['kind'] == 'PersistentVolumeClaim']:
            assert claim['metadata']['annotations']['helm.sh/resource-policy'] == 'keep'
        # No Argo wave can block waiting for a ConfigMap/PVC in a later wave.
        assert all('argocd.argoproj.io/sync-wave' not in o['metadata'].get('annotations', {}) for o in objects)


def test_reserved_runtime_identity_cannot_be_overridden_by_extra_config():
    objects = render('bundled', 'test', '--set-string', 'config.MOYAI_PUBLIC_URL=https://evil.test',
                     '--set-string', 'config.MOYAI_INSTALL_TOPOLOGY=standalone')
    config = named(objects, 'ConfigMap', 'test-moyai-config')['data']
    assert config['MOYAI_PUBLIC_URL'] == 'https://gateway.example.com/moyai'
    assert config['MOYAI_INSTALL_TOPOLOGY'] == 'split'


@pytest.mark.parametrize('value', ['https://host/moyai/../admin', 'https://host/moyai%2fadmin', 'https://host/a//b'])
def test_ambiguous_ingress_paths_are_rejected(value):
    with pytest.raises(subprocess.CalledProcessError):
        render('bundled', 'test', '--set-string', 'publicUrl=' + value)


def test_release_package_pins_the_image_from_the_same_build(tmp_path):
    digest, revision = 'sha256:' + '1' * 64, '2' * 40
    subprocess.run(['python3', str(ROOT / 'scripts/build_helm_bundle.py'), '--digest', digest,
                    '--revision', revision, '--output', str(tmp_path)], check=True)
    archive, = tmp_path.glob('moyai-*.tgz')
    with tarfile.open(archive) as bundle:
        assert yaml.safe_load(bundle.extractfile('moyai/values.yaml'))['image']['digest'] == digest
        assert yaml.safe_load(bundle.extractfile('moyai/Chart.yaml'))['appVersion'] == revision
    assert yaml.safe_load((CHART / 'values.yaml').read_text())['image']['digest'] == ''


def test_separate_broker_has_only_a_cluster_ip_and_dedicated_proxy_listener():
    objects = render('bundled', 'test', '--set-string', 'brokerUrl=https://broker.example.test/moyai')
    service = named(objects, 'Service', 'test-moyai-broker')['spec']
    assert service['type'] == 'ClusterIP' and service['publishNotReadyAddresses'] is True
    assert service['ports'] == [{'name': 'http', 'port': 8080, 'targetPort': 'broker-http'}]
    proxy = next(c for c in named(objects, 'Deployment', 'test-moyai')['spec']['template']['spec']['containers']
                 if c['name'] == 'proxy')
    assert {'name': 'broker-http', 'containerPort': 8081} in proxy['ports']
    config = named(objects, 'ConfigMap', 'test-moyai-config')['data']
    assert config['MOYAI_PUBLIC_URL'] == 'https://gateway.example.com/moyai'
    assert config['MOYAI_BROKER_URL'] == 'https://broker.example.test/moyai'
    listener = named(objects, 'ConfigMap', 'test-moyai-scripts')['data']['nginx.conf'].split('listen 8081;')[1]
    assert 'location /moyai/broker/' in listener and 'location / { return 404; }' in listener
    assert ':8787' not in listener and ':8788' not in listener
    assert [o for o in objects if o['kind'] == 'Ingress'] == [o for o in render() if o['kind'] == 'Ingress']


def test_default_does_not_expose_a_separate_listener():
    objects = render()
    assert not any(o['metadata']['name'] == 'test-moyai-broker' for o in objects)
    assert 'listen 8081;' not in named(objects, 'ConfigMap', 'test-moyai-scripts')['data']['nginx.conf']


@pytest.mark.parametrize('url', ['http://broker.test/moyai', 'https://broker.test/other',
                                 'https://user:secret@broker.test/moyai', 'https://broker.test/moyai?key=secret'])
def test_unsafe_or_incompatible_broker_chart_urls_are_rejected(url):
    with pytest.raises(subprocess.CalledProcessError):
        render('bundled', 'test', '--set-string', 'brokerUrl=' + url)
