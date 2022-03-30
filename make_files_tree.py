import asyncio
import logging
import os
import re
import shutil
import zipfile
from asyncio.exceptions import TimeoutError
from string import punctuation, whitespace
from time import time
from typing import List

import aiofiles
import aiohttp
from aiohttp import ClientConnectorError

PROTOCOL = 'https://'
ILLEGAL_PATH_CHARS = punctuation.replace('.', '') + whitespace

DYNAMIC_PART_MOCK = 'telegram-crawler'

INPUT_FILENAME = os.environ.get('INPUT_FILENAME', 'tracked_links.txt')
OUTPUT_FOLDER = os.environ.get('OUTPUT_FOLDER', 'data/')

TRANSLATIONS_EN_CATEGORY_URL_REGEX = r'/en/[a-z_]+/[a-z_]+/$'

PAGE_GENERATION_TIME_REGEX = r'<!-- page generated in .+ -->'
PAGE_API_HASH_REGEX = r'\?hash=[a-z0-9]+'
PAGE_API_HASH_TEMPLATE = f'?hash={DYNAMIC_PART_MOCK}'
PASSPORT_SSID_REGEX = r'passport_ssid=[a-z0-9]+_[a-z0-9]+_[a-z0-9]+'
PASSPORT_SSID_TEMPLATE = f'passport_ssid={DYNAMIC_PART_MOCK}'
NONCE_REGEX = r'"nonce":"[a-z0-9]+_[a-z0-9]+_[a-z0-9]+'
NONCE_TEMPLATE = f'"nonce":"{DYNAMIC_PART_MOCK}'
PROXY_CONFIG_SUB_NET_REGEX = r'\d+\.\d+:8888;'
PROXY_CONFIG_SUB_NET_TEMPLATE = 'X.X:8888;'
TRANSLATE_SUGGESTION_REGEX = r'<div class="tr-value-suggestion">(.?)+</div>'
SPARKLE_SIG_REGEX = r';sig=(.*?);'
SPARKLE_SE_REGEX = r';se=(.*?);'
SPARKLE_SIG_TEMPLATE = f';sig={DYNAMIC_PART_MOCK};'
SPARKLE_SE_TEMPLATE = f';se={DYNAMIC_PART_MOCK};'

# unsecure but so simple
CONNECTOR = aiohttp.TCPConnector(ssl=False)
TIMEOUT = aiohttp.ClientTimeout(total=10)

logging.basicConfig(format='%(message)s', level=logging.DEBUG)
logger = logging.getLogger(__name__)


async def download_file(url, path, session):
    async with session.get(url) as response:
        if response.status != 200:
            return

        async with aiofiles.open(path, mode='wb') as f:
            await f.write(await response.read())


async def get_download_link_of_latest_appcenter_release(parameterized_url: str, session: aiohttp.ClientSession):
    api_base = 'https://install.appcenter.ms/api/v0.1'
    base_url = f'{api_base}/{parameterized_url}'

    async def make_req(url):
        async with session.get(url) as response:
            if response.status != 200:
                return

            return await response.json(encoding='UTF-8')

    json = await make_req(f'{base_url}/public_releases')
    if json and json[0]:
        latest_id = json[0]['id']
    else:
        return

    json = await make_req(f'{base_url}/releases/{latest_id}')
    if json:
        return json['download_url']

    return None


async def track_additional_files(files_to_track: List[str], input_dir_name: str, output_dir_name: str, encoding='utf-8'):
    for file in files_to_track:
        filename = os.path.join(OUTPUT_FOLDER, output_dir_name, file)
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        async with aiofiles.open(filename, 'w', encoding='utf-8') as w_file:
            async with aiofiles.open(os.path.join(input_dir_name, file), 'r', encoding=encoding) as r_file:
                content = await r_file.read()
                content = re.sub(r'id=".*"', 'id="tgcrawl"', content)
                await w_file.write(content)


async def download_telegram_macos_beta_and_extract_resources(session: aiohttp.ClientSession):
    parameterized_url = 'apps/keepcoder/telegram-swift/distribution_groups/public'
    download_url = await get_download_link_of_latest_appcenter_release(parameterized_url, session)

    if not download_url:
        return

    await download_file(download_url, 'macos.zip', session)

    # synced
    with zipfile.ZipFile('macos.zip', 'r') as f:
        f.extractall('macos')

    files_to_track = [
        'Telegram.app/Contents/Resources/en.lproj/Localizable.strings',
    ]
    await track_additional_files(files_to_track, 'macos', 'telegram-beta-macos', 'utf-16')

    os.path.isdir('macos') and shutil.rmtree('macos')
    os.remove('macos.zip')


async def download_telegram_android_beta_and_extract_resources(session: aiohttp.ClientSession):
    parameterized_url = 'apps/drklo-2kb-ghpo/telegram-beta-2/distribution_groups/all-users-of-telegram-beta-2'
    download_url = await get_download_link_of_latest_appcenter_release(parameterized_url, session)

    if not download_url:
        return

    await download_file('https://bitbucket.org/iBotPeaches/apktool/downloads/apktool_2.6.1.jar', 'tool.apk', session)
    await download_file(download_url, 'android.apk', session)

    def cleanup():
        os.path.isdir('android') and shutil.rmtree('android')
        os.remove('tool.apk')
        os.remove('android.apk')

    process = await asyncio.create_subprocess_exec('java', '-jar', 'tool.apk', 'd', '-s', '-f', 'android.apk')
    await process.communicate()

    if process.returncode != 0:
        cleanup()
        return

    files_to_track = [
        'res/values/strings.xml',
        'res/values/public.xml'
    ]
    await track_additional_files(files_to_track, 'android', 'telegram-beta-android')

    cleanup()


async def collect_translations_paginated_content(url: str, session: aiohttp.ClientSession) -> str:
    headers = {'X-Requested-With': 'XMLHttpRequest'}
    content = list()

    async def _get_page(offset: int):
        logger.info(f'Url: {url}, offset: {offset}')
        data = {'offset': offset, 'more': 1}

        try:
            async with session.post(
                    f'{PROTOCOL}{url}', data=data, headers=headers, allow_redirects=False, timeout=TIMEOUT
            ) as response:
                if response.status != 200:
                    logger.debug(f'Resend cuz {response.status}')
                    return await asyncio.gather(_get_page(offset))

                json = await response.json(encoding='UTF-8')
                if 'more_html' in json and json['more_html']:
                    content.append(json['more_html'])
                    await asyncio.gather(_get_page(offset + 200))
        except (TimeoutError, ClientConnectorError):
            logger.warning(f'Client or timeout error. Retrying {url}; offset {offset}')
            await asyncio.gather(_get_page(offset))

    await _get_page(0)

    return '\n'.join(content)


async def crawl(url: str, session: aiohttp.ClientSession):
    try:
        logger.info(f'Process {url}')
        async with session.get(f'{PROTOCOL}{url}', allow_redirects=False, timeout=TIMEOUT) as response:
            if response.status // 100 == 5:
                logger.warning(f'Error 5XX. Retrying {url}')
                return await asyncio.gather(crawl(url, session))

            if response.status not in {200, 304}:
                if response.status != 302:
                    content = await response.text()
                    logger.debug(f'Skip {url} because status code == {response.status}. Content: {content}')
                return

            # bypass external slashes and so on
            url_parts = [p for p in url.split('/') if p not in ILLEGAL_PATH_CHARS]
            # handle pure domains and html pages without ext in url
            ext = '.html' if '.' not in url_parts[-1] or len(url_parts) == 1 else ''

            filename = OUTPUT_FOLDER + '/'.join(url_parts) + ext

            content = await response.text(encoding='UTF-8')
            if re.search(TRANSLATIONS_EN_CATEGORY_URL_REGEX, url):
                content = await collect_translations_paginated_content(url, session)

            os.makedirs(os.path.dirname(filename), exist_ok=True)
            async with aiofiles.open(filename, 'w', encoding='utf-8') as f:
                content = re.sub(PAGE_GENERATION_TIME_REGEX, '', content)
                content = re.sub(PAGE_API_HASH_REGEX, PAGE_API_HASH_TEMPLATE, content)
                content = re.sub(PASSPORT_SSID_REGEX, PASSPORT_SSID_TEMPLATE, content)
                content = re.sub(NONCE_REGEX, NONCE_TEMPLATE, content)
                content = re.sub(PROXY_CONFIG_SUB_NET_REGEX, PROXY_CONFIG_SUB_NET_TEMPLATE, content)
                content = re.sub(TRANSLATE_SUGGESTION_REGEX, '', content)
                content = re.sub(SPARKLE_SIG_REGEX, SPARKLE_SIG_TEMPLATE, content)
                content = re.sub(SPARKLE_SE_REGEX, SPARKLE_SE_TEMPLATE, content)

                logger.info(f'Write to {filename}')
                await f.write(content)
    except (TimeoutError, ClientConnectorError):
        logger.warning(f'Client or timeout error. Retrying {url}')
        await asyncio.gather(crawl(url, session))


async def start(url_list: set[str]):
    async with aiohttp.ClientSession(connector=CONNECTOR) as session:
        await asyncio.gather(*[crawl(url, session) for url in url_list])

        # yeap it will be called each run, and what? ;d
        await download_telegram_android_beta_and_extract_resources(session)
        await download_telegram_macos_beta_and_extract_resources(session)


if __name__ == '__main__':
    with open(INPUT_FILENAME, 'r') as f:
        tracked_urls = set([l.replace('\n', '') for l in f.readlines()])

    logger.info(f'Start crawling content of {len(tracked_urls)} tracked urls...')
    start_time = time()
    asyncio.get_event_loop().run_until_complete(start(tracked_urls))
    logger.info(f'Stop crawling content. {time() - start_time} sec.')
