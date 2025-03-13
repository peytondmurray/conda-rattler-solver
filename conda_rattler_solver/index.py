import logging
import os
from functools import partial
from pathlib import Path
from typing import Dict, Iterable, Tuple, Union

from conda.base.constants import REPODATA_FN
from conda.base.context import context
from conda.common.io import DummyExecutor, ThreadLimitedThreadPoolExecutor
from conda.common.url import percent_decode, remove_auth, split_anaconda_token
from conda.core.subdir_data import SubdirData
from conda.models.channel import Channel
from conda_libmamba_solver.index import _ChannelRepoInfo, LibMambaIndexHelper

from rattler import SparseRepoData, Channel as RattlerChannel

log = logging.getLogger(f"conda.{__name__}")


class RattlerIndexHelper(LibMambaIndexHelper):
    """An interface between conda and rattler which builds an index.

    An index is a collection of package records which can be part of a solution.
    It is built by collecting all the repodata.json files from channels and their
    subdirs. For existing environments, the installed packages are also added to
    the index (this helps with simplifying solutions and outputs). The local cache
    can also be added as a "channel", which is useful in offline mode or with no
    channels configured.
    """
    def __init__(
        self,
        channels: Iterable[Union[Channel, str]] = None,
        subdirs: Iterable[str] = None,
        repodata_fn: str = REPODATA_FN,
    ):

        if channels is None:
            channels = context.channels
        self._channels: list[Channel] = [Channel(ch) for ch in channels]

        self._subdirs = context.subdirs if subdirs is None else subdirs
        self._repodata_fn = repodata_fn

        self._index = self._load_channels()


    def get_info(self, key: str) -> _ChannelRepoInfo:
        orig_key = key
        if not key.startswith("file://"):
            # The conda functions (specifically remove_auth) assume the input
            # is a url; a file uri on windows with a drive letter messes them up.
            # For the rest, we remove all forms of authentication
            key = split_anaconda_token(remove_auth(key))[0]
        try:
            return self._index[key]
        except KeyError as exc:
            raise KeyError(
                f"Channel info for {orig_key} ({key}) not found. "
                f"Available keys: {list(self._index)}"
            ) from exc

    def _fetch_channel(self, url: str) -> Tuple[str, os.PathLike]:
        channel = Channel.from_url(url)
        if not channel.subdir:
            raise ValueError(f"Channel URLs must specify a subdir! Provided: {url}")

        if "PYTEST_CURRENT_TEST" in os.environ:
            # Workaround some testing issues - TODO: REMOVE
            # Fix conda.testing.helpers._patch_for_local_exports by removing last line
            maybe_cached = SubdirData._cache_.get((url, self._repodata_fn))
            if maybe_cached and maybe_cached._mtime == float("inf"):
                del SubdirData._cache_[(url, self._repodata_fn)]
            # /Workaround

        log.debug("Fetching %s with SubdirData.repo_fetch", channel)
        subdir_data = SubdirData(channel, repodata_fn=self._repodata_fn)
        json_path, _ = subdir_data.repo_fetch.fetch_latest_path()

        return url, json_path

    def _json_path_to_repo_info(self, url: str, json_path: str) -> _ChannelRepoInfo:
        channel = Channel.from_url(url)
        noauth_url = channel.urls(with_credentials=False, subdirs=(channel.subdir,))[0]
        json_path = Path(json_path)
        # solv_path = json_path.parent / f"{json_path.stem}.solv"
        # try:
        #     # use solv file if it's newer than the json file
        #     if json_path.stat().st_mtime <= solv_path.stat().st_mtime:
        #         json_path = solv_path
        #         log.debug("Using %s instead of %s", solv_path, json_path)
        # except OSError as exc:
        #     log.debug("Failed to stat %s or %s", solv_path, json_path, exc_info=exc)
        rattler_channel = RattlerChannel(noauth_url.rsplit("/", 1)[0])
        repo = SparseRepoData(rattler_channel, channel.subdir, json_path)
        return _ChannelRepoInfo(
            repo=repo,
            channel=channel,
            url_w_cred=url,
            url_no_cred=noauth_url,
        )

    def _load_channels(self) -> Dict[str, _ChannelRepoInfo]:
        """Load the channels.

        Authed URLs always take precedence over unauthed URLs.

        Returns
        -------
        Dict[str, _ChannelRepoInfo]
            A mapping between channel urls and objects that hold information about the repository
        """
        # 1. Obtain and deduplicate URLs from channels
        urls = []
        seen_noauth = set()
        for channel in self._channels:
            noauth_urls = channel.urls(with_credentials=False, subdirs=self._subdirs)

            # If the urls are unauth and have already been seen, do nothing
            if seen_noauth.issuperset(noauth_urls):
                continue

            # Authed channels always takes precedence over unauthed channels
            if channel.auth or channel.token:
                urls += channel.urls(with_credentials=True, subdirs=self._subdirs)
                seen_noauth |= noauth_urls
                continue

            # at this point, we are handling an unauthed channel; in some edge cases,
            # an auth'd variant of the same channel might already be present in `urls`.
            # we only add them if we haven't seen them yet
            for url in noauth_urls:
                if url not in seen_noauth:
                    urls.append(url)
                    seen_noauth.add(url)

        urls = tuple(dict.fromkeys(urls))  # de-duplicate

        # 2. Fetch URLs (if needed)
        Executor = (
            DummyExecutor
            if context.debug or context.repodata_threads == 1
            else partial(ThreadLimitedThreadPoolExecutor, max_workers=context.repodata_threads)
        )
        with Executor() as executor:
            jsons = {url: str(path) for (url, path) in executor.map(self._fetch_channel, urls)}

        # 3. Create repos in same order as `urls`
        index = {}
        for url in urls:
            info = self._json_path_to_repo_info(url, jsons[url])
            index[info.url_no_cred] = info

        return index
