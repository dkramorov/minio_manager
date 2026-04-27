import datetime
import os
import json
import traceback
import requests
import urllib3
import certifi

from minio import Minio
from minio.commonconfig import REPLACE, CopySource

from managers.simple_logger import logger


class MockSettings:
    FULL_SETTINGS_SET = os.environ


try:
    from django.conf import settings
    hasattr(settings, 'DEBUG')
except Exception as e:
    logger.info('Exception from django.conf import settings: %s' % e)
    settings = MockSettings()


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def get_minio_configs() -> dict:
    """Получение конфигураций минио из настроек
       Плохие алиасы, для которых не хватает данных будут пропущены
    """
    minio_configs = {}
    search_patterns = (
        '_MINIO_HOST',
        '_MINIO_PORT',
        '_MINIO_ACCESS_KEY',
        '_MINIO_SECRET_KEY',
        '_MINIO_SECURE',
    )
    def get_pattern_key(pattern: str):
        """Получение ключа настроек, например, _MINIO_HOST => host
        """
        return pattern.split('_MINIO_')[-1].lower()

    for key, value in settings.FULL_SETTINGS_SET.items():
        if '_MINIO_' in key:
            search_pattern = [item for item in search_patterns if item in key]
            if not search_pattern:
                continue
            search_pattern = search_pattern[0]
            alias = key.split(search_pattern)[0].lower()
            if not alias in minio_configs:
                minio_configs[alias] = {}
            param_key = get_pattern_key(pattern=search_pattern)
            minio_configs[alias][param_key] = value
    # Валидация, что всех данных хватает для алиаса
    bad_aliases = []
    for alias, data in minio_configs.items():
        for search_pattern in search_patterns:
            key = get_pattern_key(pattern=search_pattern)
            if not key in data: # data.get(key) не пройдет с _MINIO_SECURE=
                bad_aliases.append(alias)
    if bad_aliases:
        logger.info('BAD MINIO ALIASES: %s' % bad_aliases)
    return {k: v for k, v in minio_configs.items() if k not in bad_aliases}


def get_minio_managers() -> dict:
    """Создать минио-менеджеры для работы с минио
    """
    minio_managers = {}
    minio_configs = get_minio_configs()
    for alias, config in minio_configs.items():
        minio_managers[alias] = MinioManager(
            host='%s:%s' % (config['host'], config['port']),
            access_key=config['access_key'],
            secret_key=config['secret_key'],
            bucket='vol-data',
            secure=bool(config['secure']),
        )
    return minio_managers


def get_mc_client(url: str = None):
    """Получить mc client
       работает только под vpn
       #wget https://dl.minio.io/client/mc/release/linux-amd64/mc # darwin-arm64
    """
    if not url:
        url = 'https://dl.minio.io/client/mc/release/darwin-arm64/mc'
    mc_client = '/tmp/mc'
    if not os.path.exists(mc_client):
        r = requests.get(url, stream=True)
        if r.status_code == 200:
            with open(mc_client, 'wb') as f:
                for chunk in r:
                    f.write(chunk)
    os.system('chmod +x %s' % mc_client)
    return mc_client


class MinioManager:
    """Работа с хранилищем Minio
        https://min.io/docs/minio/linux/developers/python/API.html

        print('check_bucket', MinioManager().check_bucket())

        print('download',
              MinioManager().download('public_/4z/pg/a095ekkzdfy65xwas147nl20jtwr.flac',
                                      '/Users/jocker/Downloads/test.flac'))

        print('upload',
              MinioManager().upload('public_/potestua.flac', '/Users/jocker/Downloads/test.flac'))

        print('download',
              MinioManager().download('public_/potestua.flac',
                                      '/Users/jocker/Downloads/test2.flac'))

        print('drop',
              MinioManager().drop('public_/potestua.flac'))

        Есть проблема с urllib3.PoolManager(maxsize=10...
        mcedit env/lib/python3.10/site-packages/minio/api.py
        стоит переопределить init
    """

    def __init__(self,
                 host: str = None,
                 access_key: str = None,
                 secret_key: str = None,
                 bucket: str = None,
                 secure: bool = True,
                 retry = 1,
                 debug: bool = True):
        default_config = settings.MINIO_CONFIG if hasattr(settings, 'MINIO_CONFIG') else {}
        self.host = host or '%s:%s' % (default_config['host'], default_config['port'])
        self.access_key = access_key or default_config['access_key']
        self.secret_key = secret_key or default_config['secret_key']
        self.secure = secure
        self.client = Minio(
            endpoint=self.host,
            access_key=self.access_key,
            secret_key=self.secret_key,
            secure=self.secure)
        self.bucket = bucket or default_config['bucket']
        self.retry = retry
        self.debug = debug

    def get_credentials(self):
        """Получить словарь с учетными данными,
           например, для создания алиаса
        """
        return {
            'url' : 'http%s://%s' % ('s' if self.secure else '', self.host),
            'accessKey': self.access_key,
            'secretKey': self.secret_key,
            'api': 's3v4',
            'path': 'auto',
        }

    def create_mc_alias(self, alias_name: str, mc_client_url: str = None):
        """Создать алиас для минио
           :param alias_name: название алиаса для s3
           :param mc_client_url: адрес, откуда скачиваем клиент mc
        """
        mc_client = get_mc_client(url=mc_client_url)
        cred_json = '/tmp/credentials.json'
        with open(cred_json, 'w+', encoding='utf-8') as f:
            f.write(json.dumps(self.get_credentials()))
        os.system('%s alias import %s %s' % (mc_client, alias_name, cred_json))

    def check_bucket(self, with_create: bool = True, with_anonymous_policy: bool = True):
        """Создает или проверяет bucket"""
        found = self.client.bucket_exists(self.bucket)
        if not found:
            if with_create:
                self.client.make_bucket(self.bucket)
                if with_anonymous_policy:
                    self.set_bucket_policy()
                return True
        else:
            return True
        return False

    def drop(self, remote_name: str, retry: int = 0):
        """Удаление файла из хранилища
           :param remote_name: путь к файлу в хранилище
        """
        if not retry:
            retry = self.retry
        for i in range(retry):
            try:
                return self.client.remove_object(self.bucket, remote_name)
            except Exception:
                traceback.print_exc()

    def upload(self, remote_name: str, local_path: str, retry: int = 0):
        """Загрузить в хранилище файл
           :param remote_name: путь к файлу
        """
        if not retry:
            retry = self.retry
        for i in range(retry):
            try:
                with open(local_path, 'rb') as f:
                    file_stat = os.stat(local_path)
                    return {
                        'result': self.client.put_object(self.bucket, remote_name, f, file_stat.st_size),
                        'size': file_stat.st_size,
                    }
            except Exception as e:
                traceback.print_exc()

    def download(self, remote_name: str, local_path: str, retry: int = 0):
        """Скачать из хранилища файл
           :param remote_name: путь к файлу в хранилище
        """
        if not retry:
            retry = self.retry
        for i in range(retry):
            try:
                data = self.client.get_object(self.bucket, remote_name)
                with open(local_path, 'wb+') as f:
                    for stream in data.stream(32*1024):
                        f.write(stream)
                return local_path
            except Exception:
                if self.debug:
                    traceback.print_exc()

    def set_bucket_policy(self, policy: dict = None):
        """Установить политику для bucket"""
        if not policy:
            policy = {
                'Version': '2012-10-17',
                'Statement': [{
                    'Effect': 'Allow',
                    'Principal': {'AWS': '*'},
                    'Action': ['s3:GetBucketLocation', 's3:ListBucket'],
                    'Resource': 'arn:aws:s3:::%s' % self.bucket,
                }, {
                    'Effect': 'Allow',
                    'Principal': {'AWS': '*'},
                    'Action': 's3:GetObject',
                    'Resource': 'arn:aws:s3:::%s/*' % self.bucket,
                },],
            }
        return self.client.set_bucket_policy(self.bucket, json.dumps(policy))

    def copy_object(self, remote_name: str, new_remote_name: str):
        """Дублировать файл
           :param remote_name: путь к файлу в хранилище
        """
        return self.client.copy_object(
            self.bucket,
            new_remote_name,
            CopySource(self.bucket, remote_name),
        )

    def stat_object(self, remote_name: str):
        """Получение информации по объекту
           stat_object.object_name, stat_object.size
           :param remote_name: путь к файлу в хранилище
        """
        return self.client.stat_object(self.bucket, remote_name)

    def list_objects(self, remote_name: str = None):
        if not remote_name:
            remote_name = ''
        return self.client.list_objects(
            self.bucket,
            prefix=remote_name,
            recursive=False,
        )

    def get_buckets(self):
        """Список бакетов"""
        return self.client.list_buckets()

    def get_bucket_versioning(self):
        """Получить статус версионирования для бакета"""
        return self.client.get_bucket_versioning(self.bucket).status

    def get_bucket_lifecycle(self):
        """Получить жизненный цикл для бакета"""
        return self.client.get_bucket_lifecycle(self.bucket)

    def get_link(self,
                 remote_name: str,
                 expires: datetime.timedelta = datetime.timedelta(hours=1)):
        """Ссылка для скачивания на файл
           :param remote_name: путь к файлу в хранилище
           :param expires: истечение срока действия публичной ссылки
        """
        return self.client.get_presigned_url(
            'GET',
            self.bucket,
            remote_name,
            expires=expires,
        )

    @staticmethod
    def recursive_copy(mm_src, mm_dst, remote_name: str = ''):
        """Рекурсивное копирование из одного s3 в другое через локальную папку
           TODO: параллельно в потоках
           :param mm_src: менеджер минио источник
           :param mm_dst: менеджер минио приемник
           :param remote_name: путь к папке с которой начинаем рекурсивный обход
        """
        for obj in mm_src.list_objects(remote_name=remote_name):
            if obj.is_dir:
                logger.info('DIR: %s' % obj.object_name)
                MinioManager.recursive_copy(mm_src=mm_src, mm_dst=mm_dst, remote_name=obj.object_name)
            else:
                try:
                    mm_dst.stat_object(remote_name=obj.object_name)
                    logger.info('already exists %s' % obj.object_name)
                    continue
                except Exception:
                    logger.info('prepare for copy %s' % obj.object_name)

                file_name = obj.object_name.split('/')[-1]
                local_path = '/tmp/%s' % file_name
                mm_src.download(remote_name=obj.object_name, local_path=local_path)
                mm_dst.upload(remote_name=obj.object_name, local_path=local_path)
                os.unlink(local_path)
                logger.info('%s copied' % obj.object_name)

    def vol_data_script(self):
        """Загружает данные по месту на диске в bucket=vol-data
           USAGE: vol_data.sh minio-archive-3.prod.infra... 9003 key secret /DISK2
        """
        return """
#!/bin/bash

bucket='vol-data'

host=$1
MINIO_PORT=$2
s3_key=$3
s3_secret=$4
disk_path=$5

prefix='s'

mkdir -p /tmp/metrics
mkdir -p /tmp/metrics$disk_path
file=vol_data.txt
df $disk_path > /tmp/metrics$disk_path/$file
cd /tmp/metrics$disk_path

resource="/${bucket}/${file}"
content_type="application/octet-stream"
date=`date -R`
_signature="PUT\n\n${content_type}\n${date}\n${resource}"
signature=`echo -en ${_signature} | openssl sha1 -hmac ${s3_secret} -binary | base64`

curl -k -m 20 -v -X PUT -T "${file}" \
          -H "Host: $host" \
          -H "Date: ${date}" \
          -H "Content-Type: ${content_type}" \
          -H "Authorization: AWS ${s3_key}:${signature}" \
          http$prefix://$host:$MINIO_PORT${resource}
    """.stript()