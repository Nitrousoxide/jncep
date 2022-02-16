from functools import partial
import logging
import sys

import attr
import dateutil
import trio

from . import core, jncweb, spec, track, utils
from .trio_utils import background, gather

logger = logging.getLogger(__package__)
console = utils.getConsole()


@attr.s
class UpdateResult:
    is_error = attr.ib(False)
    series = attr.ib(None)
    is_updated = attr.ib(None)
    is_considered = attr.ib(True)


async def update_url_series(
    session,
    jnc_url,
    epub_generation_options,
    tracked_series,
    is_sync,
    new_synced,
    is_whole_volume,
):
    # for single url => if error no catch : let it crash and report to the user

    jnc_resource = jncweb.resource_from_url(jnc_url)
    series_meta = await core.resolve_series(session, jnc_resource)

    series_url = jncweb.url_from_series_slug(series_meta.raw_data.slug)

    if series_url not in tracked_series:
        console.warning(
            f"The series '{series_meta.raw_data.title}' is not tracked! "
            f"Use the 'jncep track add' command first."
        )
        return

    if is_sync:
        # not very useful but make it possible
        # only consider newly synced series if --sync used
        # to mirror case with no URL argument
        if series_url not in new_synced:
            console.warning(
                f"The series '{series_meta.raw_data.title}' is not among the "
                f"tracked series added from syncing. Use 'jncep update' "
                "without --sync."
            )
            return

    series_details = tracked_series[series_url]

    is_force_from_beginning = is_sync
    is_updated = await _create_epub_for_new_parts(
        session,
        series_details,
        series_meta,
        epub_generation_options,
        is_whole_volume,
        is_force_from_beginning,
    )

    updated_series = {}
    if is_updated:
        console.info(
            f"The series '{series_meta.raw_data.title}' has been updated!",
            style="success",
        )
        updated_series[series_url] = series_meta

    # meta not error like with all_series
    return updated_series, series_meta


async def update_all_series(
    session,
    epub_generation_options,
    tracked_series,
    is_sync,
    new_synced,
    is_whole_volume,
):
    async with trio.open_nursery() as n:
        series_urls = []
        f_series = []
        for series_url, series_details in tracked_series.items():
            f_one_series = background(
                n,
                partial(
                    _handle_series,
                    session,
                    series_url,
                    series_details,
                    epub_generation_options,
                    is_sync,
                    new_synced,
                    is_whole_volume,
                ),
            )
            f_series.append(f_one_series)
            series_urls.append(series_url)

        results = await gather(n, f_series).get()

        # FIXME rework the event / logging to the user
        updated_series = {}
        error_series = []

        update_result: UpdateResult
        for i, update_result in enumerate(results):
            if not update_result.is_considered:
                continue

            series_url = series_urls[i]

            if update_result.is_updated:
                updated_series[series_url] = update_result.series

            if update_result.is_error:
                error_series.append(tracked_series[series_url])

        return updated_series, error_series


async def _handle_series(
    session,
    series_url,
    series_details,
    epub_generation_options,
    is_sync,
    new_synced,
    is_whole_volume,
):
    try:
        if is_sync and series_url not in new_synced:
            return UpdateResult(is_considered=False)

        jnc_resource = jncweb.resource_from_url(series_url)
        series_meta = await core.resolve_series(session, jnc_resource)

        # generate from the start if the series is newly sync
        is_force_from_beginning = is_sync
        is_updated = await _create_epub_for_new_parts(
            session,
            series_details,
            series_meta,
            epub_generation_options,
            is_whole_volume,
            is_force_from_beginning,
        )

        if is_updated:
            # TODO event
            console.info(
                f"The series '{series_meta.raw_data.title}' has been updated!",
                style="success",
            )

        return UpdateResult(False, series_meta, is_updated)

    except Exception as ex:
        # FIXME show the user some feedback as to the nature of the error
        logger.debug(f"Error _handle_series: {ex}", exc_info=sys.exc_info())
        return UpdateResult(True)


# TODO too complex ; refactor
async def _create_epub_for_new_parts(
    session,
    series_details,
    series_meta,
    epub_generation_options,
    is_whole_volume=False,
    is_force_from_beginning=False,
):
    if series_details.part == 0 or is_force_from_beginning:
        # Firt clause: special processing : means there was no part available when the
        # series was started tracking

        # will fetch one part ; sufficient for here
        # quick to check
        await track.fill_meta_for_track(session, series_meta)
        parts = core.all_parts_meta(series_meta)

        # still no part ?
        if not parts:
            return False
        else:
            console.info(f"Series '{series_meta.raw_data.title}' will be updated...")

            # complete series from beginning
            await core.fill_meta(session, series_meta)

            part_filter = partial(core.is_part_available, session.now)

            (
                volumes_to_download,
                parts_to_download,
            ) = core.relevant_volumes_and_parts_for_content(series_meta, part_filter)
            volumes_for_cover = core.relevant_volumes_for_cover(
                volumes_to_download, epub_generation_options.is_by_volume
            )

            await core.fill_covers_and_content(
                session, volumes_for_cover, parts_to_download
            )
            await core.create_epub(
                series_meta,
                volumes_to_download,
                parts_to_download,
                epub_generation_options,
            )

            return True
    else:

        if not series_details.part_date:
            # TODO test this branch
            # if here => old format, first lookup date of last part and use that
            # maybe still useful for stalled series so keep it
            part_spec = spec.analyze_part_specs(series_details.part)
            await core.fill_meta(session, series_meta, part_spec.has_volume)
            parts = core.all_parts_meta(series_meta)
            for part in parts:
                if part_spec.has_part(part):
                    # will be filled if the part still exists (it should)
                    # TODO case it doesn't ? eg tracked.json filled by hand
                    last_update_part = part
                    break
            # in UTC
            last_update_date = last_update_part.raw_data.launch
        else:
            # new format : date is recorded
            last_update_date = series_details.part_date

        last_update_date = dateutil.parser.parse(last_update_date)

        await fill_meta_for_update(session, series_meta, last_update_date)
        # normally if in this branch parts should not be empty
        parts = core.all_parts_meta(series_meta)

        # TOC is below a part in the Labs API
        last_part = parts[-1]
        toc = await session.api.fetch_data("parts", last_part.part_id, "toc")
        # weird struct for the response : toc.parts has pagination struct (but all
        # parts seem to be there anyway) and parts property in turn
        parts_id_to_download = _filter_parts_released_after_date(
            last_update_date, toc.parts.parts
        )

        if not parts_id_to_download:
            # not updated
            return False

        console.info(f"Series '{series_meta.raw_data.title}' will be updated...")

        def simple_part_filter(part):
            return part.part_id in parts_id_to_download and core.is_part_available(
                session.now, part
            )

        (
            volumes_to_download,
            parts_to_download,
        ) = core.relevant_volumes_and_parts_for_content(series_meta, simple_part_filter)

        if is_whole_volume:
            # second pass : filter on the volumes_to_download
            # all the parts of those volumes must be downloaded
            volumes_id_to_download = set((v.volume_id for v in volumes_to_download))

            def whole_volume_part_filter(part):
                return (
                    part.volume.volume_id in volumes_id_to_download
                    and core.is_part_available(session.now, part)
                )

            (
                volumes_to_download,
                parts_to_download,
            ) = core.relevant_volumes_and_parts_for_content(
                series_meta, whole_volume_part_filter
            )

        # FIXME also log some have expired
        if not parts_to_download:
            # TODO event
            console.warning(
                f"All updated parts for '{series_meta.raw_data.title}' have expired!"
            )
            raise core.NoRequestedPartAvailableError(series_meta.raw_data.slug)

        volumes_for_cover = core.relevant_volumes_for_cover(
            volumes_to_download, epub_generation_options.is_by_volume
        )

        await core.fill_covers_and_content(
            session, volumes_for_cover, parts_to_download
        )
        await core.create_epub(
            series_meta,
            volumes_to_download,
            parts_to_download,
            epub_generation_options,
        )

        return True


async def fill_meta_for_update(session, series, last_update_date):
    volumes = await core.fetch_volumes_meta(session, series.series_id)
    series.volumes = volumes
    for volume in volumes:
        volume.series = series

    # in order not to do too many requests:
    # first try last 2 volumes => in most update scenarios (if updated frequently),
    # this should be enough
    # also special case of multiple volumes published at the same time (altenia)
    # => 2 volumes ; but will fail if more
    # TODO detect ? or add a switch to bypass (ie all the volumes)
    last_2_volumes = volumes[-2:]
    rest_volumes = volumes[:-2]
    await core.fill_parts_meta_for_volumes(session, last_2_volumes)
    for volume in last_2_volumes:
        for part in volume.parts:
            # check if has part before the reference date
            # => means the parts relased after are all there
            # (except possibly when multiple volumes > 2 published at the same time)
            if not _is_released_after_date(last_update_date, part.raw_data.launch):
                break
        else:
            continue
        break
    else:
        # just give up and request everything
        await core.fill_parts_meta_for_volumes(session, rest_volumes)


def _is_released_after_date(date, part_date_s):
    launch_date = dateutil.parser.parse(part_date_s)
    return launch_date > date


def _filter_parts_released_after_date(date, parts):
    # parts is the raw Part struct from JNC Labs API
    parts_id_to_download = set()
    for part in parts:
        # all date strings are in ISO format
        # so no need to parse really
        # parsing just to be safe
        # in case different shape like ms part or not (which throws str comp off)
        if _is_released_after_date(date, part.launch):
            parts_id_to_download.add(part.legacyId)

    return parts_id_to_download
