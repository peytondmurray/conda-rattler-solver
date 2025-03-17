import logging
from pathlib import Path

import rattler
from conda.base.constants import ChannelPriority
from conda.models.channel import Channel
from conda.models.match_spec import MatchSpec
from conda.models.records import PackageRecord

logger = logging.getLogger(f"conda.{__name__}")

def convert_channel_priority(priority: ChannelPriority) -> rattler.ChannelPriority:
    """Convert a conda channel priority to its rattler counterpart.

    Parameters
    ----------
    priority : ChannelPriority
        Channel priority to convert

    Returns
    -------
    rattler.ChannelPriority
        Rattler ChannelPriority object generated from the input conda channel priority
    """
    if priority == ChannelPriority.STRICT:
        return rattler.ChannelPriority.Strict

    if priority == ChannelPriority.FLEXIBLE:
        logger.warning(
            "Rattler does not support flexible channel priority. "
            "Using strict channel priority instead."
        )
        return rattler.ChannelPriority.Strict

    return rattler.ChannelPriority.Disabled


def convert_channels(channels: list[Channel]) -> list[rattler.Channel]:
    """Convert a list of conda channels to their rattler counterparts.

    Parameters
    ----------
    channels : list[Channel]
        Conda channels to convert

    Returns
    -------
    list[rattler.Channel]
        Rattler Channel objects generated from the conda Channel objects
    """
    result = []
    for channel in channels:
        result.append(rattler.Channel(name=channel.name))
    return result


def convert_spec(specs: list[MatchSpec]) -> list[rattler.MatchSpec]:
    """Convert a list of conda specs to their rattler counterparts.

    Parameters
    ----------
    specs : list[MatchSpec]
        Conda specs to convert

    Returns
    -------
    list[rattler.MatchSpec]
        Rattler MatchSpec objects generated from the conda MatchSpec objects
    """
    result = []
    for spec in specs:
        result.append(rattler.MatchSpec(str(spec)))
    return result


# def convert_record(specs: list[MatchSpec]) -> list[rattler.RepoDataRecord]:
#     result = []
#     for spec in specs:
#         result.append(
#             rattler.RepoDataRecord(
#                 package_record=rattler.PackageRecord(),
#             )
#         )
#
#     pkg_record = rattler.PackageRecord(
#         arch=None,
#         build=self.build,
#         build_number=self.build_number,
#         name=self.name,
#         platform=None,
#         subdir=self.subdir,
#         version=self.version,
#     )
#     return rattler.RepoDataRecord(
#         channel=self.conda_channel,
#         file_name=self.url.split("/")[-1],
#         package_record=pkg_record,
#         url=self.url,
#     )
#
#     return result


def get_installed(prefix: Path | str) -> list[rattler.PrefixRecord]:
    """Get a list of PrefixRecord objects for the installed packages in the prefix.

    Parameters
    ----------
    prefix : Path | str
        Prefix to search for installed packages

    Returns
    -------
    list[rattler.PrefixRecord]
        The installed packages for the given prefix
    """
    result = []
    for path in Path(prefix).glob('*.json'):
        result.append(rattler.PrefixRecord.from_path(path))
    return result

def rattler_repodatarecords_to_conda(
    records: list[rattler.RepoDataRecord]
) -> dict[str, PackageRecord]:
    """Convert a list of rattler.RepoDataRecord objects to their conda counterparts.

    Parameters
    ----------
    records : list[rattler.RepoDataRecord]
        List of rattler.RepoDataRecord objects to convert

    Returns
    -------
    dict[str, PackageRecord]
        A mapping between {package name: PackageRecord}, where the items in the dict
        are valid conda PackageRecord objects
    """
    result = {}
    for record in records:
        result[record.name.normalized] = PackageRecord(
            arch=record.arch,
            build=record.build,
            build_number=record.build_number,
            channel=record.channel,
            constrains=record.constrains or (),
            # date=record.date, #! TODO: MISSING
            depends=record.depends or (),
            features=record.features or (),
            fn=record.file_name,
            legacy_bz2_md5=record.legacy_bz2_md5.hex() if record.legacy_bz2_md5 else None,
            legacy_bz2_size=record.legacy_bz2_size,
            license=record.license,
            license_family=record.license_family,
            md5=record.md5.hex() if record.md5 else None,
            name=record.name.source,
            # noarch=record.noarch,  #! TODO: MISSING
            # package_type=record.package_type, #! TODO: MISSING
            platform=record.platform,
            # preferred_env=record.preferred_env, #! TODO: MISSING
            sha256=record.sha256.hex() if record.sha256 else None,
            size=record.size or 0,
            subdir=record.subdir,
            timestamp=record.timestamp.toordinal() if record.timestamp else 0,
            track_features=record.track_features or (),
            url=record.url,
            version=str(record.version),
        )
    return result
