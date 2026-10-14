"""Validate the actual Compose model without starting containers or loading secrets."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def compose_config(tmp_path):
    docker = shutil.which('docker')
    if not docker:
        pytest.skip('Docker Compose CLI is required; no daemon is needed')
    # Never expand the developer's .env.local or inherited deployment credentials.
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(('COMPOSE_', 'DB_', 'MINIO_', 'MILVUS_'))}
    values = {'DB_NAME': 'agent', 'DB_USER': 'compose_test',
              'DB_PASSWORD': 'test-$literal-&-not-a-real-password',
              'MINIO_ACCESS_KEY': 'compose_test',
              'MINIO_SECRET_KEY': 'not-a-real-storage-password',
              'MINIO_IMAGE': 'minio/minio:verified-test-image'}

    def render(missing=None, overrides=None):
        settings = values | (overrides or {})
        if missing:
            del settings[missing]
        source = tmp_path/'compose.env'
        source.write_text(''.join(f"{k}='{v}'\n" for k, v in settings.items()), encoding='utf-8')
        return subprocess.run([docker, 'compose', '-f', str(ROOT/'docker-compose.yml'),
                               '--env-file', str(source), 'config', '--format', 'json'],
                              env=env, capture_output=True, text=True, encoding='utf-8',
                              timeout=30)
    return render


def test_compose_keeps_company_data_private_and_bootstraps_real_schema(compose_config):
    result = compose_config()
    assert result.returncode == 0, result.stderr
    model = json.loads(result.stdout)
    assert model['name'] == 'project01'
    services = model['services']
    assert set(services) == {'postgres', 'etcd', 'minio', 'milvus'}
    for name, service in services.items():
        assert service['container_name'] == 'project01-'+name
        assert service['restart'] == 'unless-stopped'
        assert service['healthcheck']['test']
        for port in service.get('ports', []):
            assert port['host_ip'] == '127.0.0.1'
        assert model['volumes'][name+'_data'] == {
            'external': True, 'name': 'project01-'+name+'-data'}
    assert not services['minio'].get('ports')
    assert not services['etcd'].get('ports')
    assert model['networks']['dependencies']['external'] is True
    assert model['networks']['dependencies']['name'] == 'project01-network'
    mount = next(v for v in services['postgres']['volumes'] if v['type'] == 'bind')
    assert Path(mount['source']).resolve() == ROOT/'scripts/init_db_package.sql'
    assert Path(mount['source']).is_file()
    assert mount['read_only'] is True
    # Compose's JSON encoder omits the default false value.
    assert mount['bind'].get('create_host_path', False) is False
    # Serialized Compose config re-escapes dollars for subsequent consumption.
    password = services['postgres']['environment']['POSTGRES_PASSWORD']
    assert password.replace('$$', '$') == 'test-$literal-&-not-a-real-password'
    assert '$$POSTGRES_USER' in services['postgres']['healthcheck']['test'][1]
    for dependency in ('etcd', 'minio'):
        assert services['milvus']['depends_on'][dependency]['condition'] == 'service_healthy'


@pytest.mark.parametrize('missing', ['DB_USER', 'DB_PASSWORD', 'MINIO_IMAGE',
                                     'MINIO_ACCESS_KEY', 'MINIO_SECRET_KEY'])
def test_compose_rejects_missing_deployment_credentials(compose_config, missing):
    result = compose_config(missing=missing)
    assert result.returncode != 0
    assert missing in result.stderr


def test_compose_host_ports_follow_backend_configuration(compose_config):
    result = compose_config(overrides={'DB_PORT': '15433', 'MILVUS_PORT': '29531'})
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)['services']
    assert str(services['postgres']['ports'][0]['published']) == '15433'
    assert str(services['milvus']['ports'][0]['published']) == '29531'
