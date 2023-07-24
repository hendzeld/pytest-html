# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
import datetime
import json
import math
import os
import re
import warnings
from pathlib import Path

import pytest
from pytest_metadata.plugin import metadata_key

from pytest_html import __version__
from pytest_html import extras
from pytest_html.util import cleanup_unserializable


class BaseReport:
    def __init__(self, report_path, config, report_data, template, css):
        self._report_path = Path(os.path.expandvars(report_path)).expanduser()
        self._report_path.parent.mkdir(parents=True, exist_ok=True)
        self._config = config
        self._template = template
        self._css = css
        self._max_asset_filename_length = int(
            config.getini("max_asset_filename_length")
        )

        self._report = report_data
        self._report.title = self._report_path.name

    @property
    def css(self):
        # implement in subclasses
        return

    def _asset_filename(self, test_id, extra_index, test_index, file_extension):
        return "{}_{}_{}.{}".format(
            re.sub(r"[^\w.]", "_", test_id),
            str(extra_index),
            str(test_index),
            file_extension,
        )[-self._max_asset_filename_length :]

    def _generate_report(self, self_contained=False):
        generated = datetime.datetime.now()
        rendered_report = self._render_html(
            generated.strftime("%d-%b-%Y"),
            generated.strftime("%H:%M:%S"),
            __version__,
            self.css,
            self_contained=self_contained,
            test_data=cleanup_unserializable(self._report.data),
            table_head=self._report.data["resultsTableHeader"],
            prefix=self._report.data["additionalSummary"]["prefix"],
            summary=self._report.data["additionalSummary"]["summary"],
            postfix=self._report.data["additionalSummary"]["postfix"],
        )

        self._write_report(rendered_report)

    def _generate_environment(self, metadata_key):
        metadata = self._config.stash[metadata_key]
        for key in metadata.keys():
            value = metadata[key]
            if self._is_redactable_environment_variable(key):
                black_box_ascii_value = 0x2593
                metadata[key] = "".join(chr(black_box_ascii_value) for _ in str(value))

        return metadata

    def _is_redactable_environment_variable(self, environment_variable):
        redactable_regexes = self._config.getini("environment_table_redact_list")
        for redactable_regex in redactable_regexes:
            if re.match(redactable_regex, environment_variable):
                return True

        return False

    def _data_content(self, *args, **kwargs):
        pass

    def _media_content(self, *args, **kwargs):
        pass

    def _process_extras(self, report, test_id):
        test_index = hasattr(report, "rerun") and report.rerun + 1 or 0
        report_extras = getattr(report, "extras", [])
        for extra_index, extra in enumerate(report_extras):
            content = extra["content"]
            asset_name = self._asset_filename(
                test_id.encode("utf-8").decode("unicode_escape"),
                extra_index,
                test_index,
                extra["extension"],
            )
            if extra["format_type"] == extras.FORMAT_JSON:
                content = json.dumps(content)
                extra["content"] = self._data_content(
                    content, asset_name=asset_name, mime_type=extra["mime_type"]
                )

            if extra["format_type"] == extras.FORMAT_TEXT:
                if isinstance(content, bytes):
                    content = content.decode("utf-8")
                extra["content"] = self._data_content(
                    content, asset_name=asset_name, mime_type=extra["mime_type"]
                )

            if extra["format_type"] in [extras.FORMAT_IMAGE, extras.FORMAT_VIDEO]:
                extra["content"] = self._media_content(
                    content, asset_name=asset_name, mime_type=extra["mime_type"]
                )

        return report_extras

    def _render_html(
        self,
        date,
        time,
        version,
        styles,
        self_contained,
        test_data,
        table_head,
        summary,
        prefix,
        postfix,
    ):
        return self._template.render(
            date=date,
            time=time,
            version=version,
            styles=styles,
            self_contained=self_contained,
            test_data=json.dumps(test_data),
            table_head=table_head,
            summary=summary,
            prefix=prefix,
            postfix=postfix,
        )

    def _write_report(self, rendered_report):
        with self._report_path.open("w", encoding="utf-8") as f:
            f.write(rendered_report)

    @pytest.hookimpl(trylast=True)
    def pytest_sessionstart(self, session):
        self._report.set_data("environment", self._generate_environment(metadata_key))

        session.config.hook.pytest_html_report_title(report=self._report)

        headers = self._report.data["resultsTableHeader"]
        session.config.hook.pytest_html_results_table_header(cells=headers)
        self._report.data["resultsTableHeader"] = _fix_py(headers)

        self._report.set_data("runningState", "Started")
        self._generate_report()

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session):
        session.config.hook.pytest_html_results_summary(
            prefix=self._report.data["additionalSummary"]["prefix"],
            summary=self._report.data["additionalSummary"]["summary"],
            postfix=self._report.data["additionalSummary"]["postfix"],
            session=session,
        )
        self._report.set_data("runningState", "Finished")
        self._generate_report()

    @pytest.hookimpl(trylast=True)
    def pytest_terminal_summary(self, terminalreporter):
        terminalreporter.write_sep(
            "-",
            f"Generated html report: file://{self._report_path.resolve().as_posix()}",
        )

    @pytest.hookimpl(trylast=True)
    def pytest_collection_finish(self, session):
        self._report.set_data("collectedItems", len(session.items))

    @pytest.hookimpl(trylast=True)
    def pytest_runtest_logreport(self, report):
        if hasattr(report, "duration_formatter"):
            warnings.warn(
                "'duration_formatter' has been removed and no longer has any effect!",
                DeprecationWarning,
            )

        data = {
            "result": _process_outcome(report),
            "duration": _format_duration(report.duration),
        }

        total_duration = self._report.data["totalDuration"]
        total_duration["total"] += report.duration
        total_duration["formatted"] = _format_duration(total_duration["total"])

        test_id = report.nodeid
        if report.when != "call":
            test_id += f"::{report.when}"
        data["testId"] = test_id

        data["extras"] = self._process_extras(report, test_id)
        links = [
            extra
            for extra in data["extras"]
            if extra["format_type"] in ["json", "text", "url"]
        ]
        cells = [
            f'<td class="col-result">{data["result"]}</td>',
            f'<td class="col-name">{data["testId"]}</td>',
            f'<td class="col-duration">{data["duration"]}</td>',
            f'<td class="col-links">{_process_links(links)}</td>',
        ]

        self._config.hook.pytest_html_results_table_row(report=report, cells=cells)
        if not cells:
            return

        cells = _fix_py(cells)
        data["resultsTableRow"] = cells

        processed_logs = _process_logs(report)
        self._config.hook.pytest_html_results_table_html(
            report=report, data=processed_logs
        )

        if self._report.add_test(data, report, processed_logs):
            self._generate_report()


def _format_duration(duration):
    if duration < 1:
        return "{} ms".format(round(duration * 1000))

    hours = math.floor(duration / 3600)
    remaining_seconds = duration % 3600
    minutes = math.floor(remaining_seconds / 60)
    remaining_seconds = remaining_seconds % 60
    seconds = round(remaining_seconds)

    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _is_error(report):
    return report.when in ["setup", "teardown"] and report.outcome == "failed"


def _process_logs(report):
    log = []
    if report.longreprtext:
        log.append(report.longreprtext.replace("<", "&lt;").replace(">", "&gt;") + "\n")
    # Don't add captured output to reruns
    if report.outcome != "rerun":
        for section in report.sections:
            header, content = section
            log.append(f"{' ' + header + ' ':-^80}\n{content}")

            # weird formatting related to logs
            if "log" in header:
                log.append("")
                if "call" in header:
                    log.append("")
    if not log:
        log.append("No log output captured.")
    return log


def _process_outcome(report):
    if _is_error(report):
        return "Error"
    if hasattr(report, "wasxfail"):
        if report.outcome in ["passed", "failed"]:
            return "XPassed"
        if report.outcome == "skipped":
            return "XFailed"

    return report.outcome.capitalize()


def _process_links(links):
    a_tag = '<a target="_blank" href="{content}" class="col-links__extra {format_type}">{name}</a>'
    return "".join([a_tag.format_map(link) for link in links])


def _fix_py(cells):
    # backwards-compat
    new_cells = []
    for html in cells:
        if not isinstance(html, str):
            if html.__module__.startswith("py."):
                warnings.warn(
                    "The 'py' module is deprecated and support "
                    "will be removed in a future release.",
                    DeprecationWarning,
                )
            html = str(html)
            html = html.replace("col=", "data-column-type=")
        new_cells.append(html)
    return new_cells
