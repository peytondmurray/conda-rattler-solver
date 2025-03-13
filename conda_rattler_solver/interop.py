import logging
from conda.models.channel import Channel
from conda.models.match_spec import MatchSpec
from conda.base.constants import ChannelPriority
import rattler

logger = logging.getLogger(f"conda.{__name__}")

def convert_channel_priority(priority: ChannelPriority) -> rattler.ChannelPriority:
    if priority == ChannelPriority.STRICT:
        return rattler.ChannelPriority.Strict
    elif priority == ChannelPriority.FLEXIBLE:
        logger.warning(
            "Rattler does not support flexible channel priority. "
            "Using strict channel priority instead."
        )
        return rattler.ChannelPriority.Strict
    else:
        return rattler.ChannelPriority.Disabled


def convert_channels(channels: list[Channel]) -> list[rattler.Channel]:
    result = []
    for channel in channels:
        result.append(rattler.Channel(name=channel.name))
    return result


def convert_spec(specs: list[MatchSpec]) -> list[rattler.MatchSpec]:
    result = []
    for spec in specs:
        result.append(rattler.MatchSpec(str(spec)))
    return result


def convert_record(specs: list[MatchSpec]) -> list[rattler.RepoDataRecord]:
    result = []
    for spec in specs:
        result.append(
            rattler.RepoDataRecord(
                package_record=rattler.PackageRecord(),
            )
        )
    return result
