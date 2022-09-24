
from contextlib import contextmanager
from copy import deepcopy
import yaml
from collections import abc
from dataclasses import asdict as dtc_asdict
import tempfile
import os.path
from typing import Any, Iterator, List, Sequence, Tuple, Callable
from prometheus_client import REGISTRY

from dlt.common import json, sleep, signals, logger
from dlt.common.runners import pool_runner as runner, TRunMetrics, initialize_runner
from dlt.common.configuration import PoolRunnerConfiguration, make_configuration
from dlt.common.file_storage import FileStorage
from dlt.common.schema import Schema
from dlt.common.typing import DictStrAny, StrAny
from dlt.common.utils import uniq_id, is_interactive
from dlt.common.sources import DLT_METADATA_FIELD, TItem, with_table_name

from dlt.extract.extractor_storage import ExtractorStorageBase
from dlt.load.client_base import SqlClientBase, SqlJobClientBase
from dlt.normalize.configuration import configuration as normalize_configuration
from dlt.load.configuration import configuration as loader_configuration
from dlt.normalize import Normalize
from dlt.load import Load
from dlt.pipeline.exceptions import MissingDependencyException, NoPipelineException, PipelineStepFailed, CannotRestorePipelineException, SqlClientNotAvailable
from dlt.pipeline.typing import PipelineCredentials


class Pipeline:
    def __init__(self, pipeline_name: str, log_level: str = "INFO") -> None:
        self.pipeline_name = pipeline_name
        self.root_path: str = None
        self.export_schema_path: str = None
        self.import_schema_path: str = None
        self.root_storage: FileStorage = None
        self.credentials: PipelineCredentials = None
        self.extractor_storage: ExtractorStorageBase = None
        self.default_schema_name: str = None
        self.state: DictStrAny = {}

        # addresses of pipeline components to be verified before they are run
        self._normalize_instance: Normalize = None
        self._loader_instance: Load = None

        # patch config and initialize pipeline
        self.C = make_configuration(PoolRunnerConfiguration, PoolRunnerConfiguration, initial_values={
            "PIPELINE_NAME": pipeline_name,
            "LOG_LEVEL": log_level,
            "POOL_TYPE": "None",
            "IS_SINGLE_RUN": True,
            "WAIT_RUNS": 0,
            "EXIT_ON_EXCEPTION": True,
        })
        initialize_runner(self.C)

    def create_pipeline(
        self,
        credentials: PipelineCredentials,
        working_dir: str = None,
        schema: Schema = None,
        import_schema_path: str = None,
        export_schema_path: str = None
    ) -> None:
        # initialize root storage
        if not working_dir:
            working_dir = tempfile.mkdtemp()
        self.root_storage = FileStorage(working_dir, makedirs=True)
        self.export_schema_path = export_schema_path
        self.import_schema_path = import_schema_path

        # check if directory contains restorable pipeline
        try:
            self._restore_state()
            # wipe out the old pipeline
            self.root_storage.delete_folder("", recursively=True)
            self.root_storage.create_folder("")
        except FileNotFoundError:
            pass

        self.root_path = self.root_storage.storage_path
        self.credentials = credentials
        self._load_modules()
        self.extractor_storage = ExtractorStorageBase(
            "1.0.0",
            True,
            FileStorage(os.path.join(self.root_path, "extractor"), makedirs=True),
            self._normalize_instance.normalize_storage)
        # create new schema if no default supplied
        if schema is None:
            # try to load schema, that will also import it
            schema_name = self.pipeline_name
            try:
                schema = self._normalize_instance.schema_storage.load_schema(schema_name)
            except FileNotFoundError:
                # create new empty schema
                schema = Schema(schema_name)
        # initialize empty state, this must be last operation when creating pipeline so restore reads only fully created ones
        with self._managed_state():
            self.state = {
                # "default_schema_name": default_schema_name,
                "pipeline_name": self.pipeline_name,
                # TODO: must come from resolved configuration
                "loader_client_type": credentials.CLIENT_TYPE,
                # TODO: must take schema prefix from resolved configuration
                "loader_schema_prefix": credentials.default_dataset
            }
        # persist schema with the pipeline
        self.set_default_schema(schema)

    def restore_pipeline(
        self,
        credentials: PipelineCredentials,
        working_dir: str,
        import_schema_path: str = None,
        export_schema_path: str = None
    ) -> None:
        try:
            # do not create extractor dir - it must exist
            self.root_storage = FileStorage(working_dir, makedirs=False)
            # restore state, this must be a first operation when restoring pipeline
            try:
                self._restore_state()
            except FileNotFoundError:
                raise CannotRestorePipelineException(f"Cannot find a valid pipeline in {working_dir}")
            restored_name = self.state["pipeline_name"]
            if self.pipeline_name != restored_name:
                raise CannotRestorePipelineException(f"Expected pipeline {self.pipeline_name}, found {restored_name} pipeline instead")
            self.default_schema_name = self.state["default_schema_name"]
            if not credentials.default_dataset:
                credentials.default_dataset = self.state["loader_schema_prefix"]
            self.root_path = self.root_storage.storage_path
            self.credentials = credentials
            self.export_schema_path = export_schema_path
            self.import_schema_path = import_schema_path
            self._load_modules()
            # schema must exist
            try:
                self.get_default_schema()
            except (FileNotFoundError):
                raise CannotRestorePipelineException(f"Default schema with name {self.default_schema_name} not found")
            self.extractor_storage = ExtractorStorageBase(
                "1.0.0",
                True,
                FileStorage(os.path.join(self.root_path, "extractor"), makedirs=False),
                self._normalize_instance.normalize_storage
            )
        except CannotRestorePipelineException:
            raise

    def extract(self, items: Iterator[TItem], schema_name: str = None, table_name: str = None) -> None:
        # check if iterator or iterable is supported
        # if isinstance(items, str) or isinstance(items, dict) or not
        # TODO: check if schema exists
        with self._managed_state():
            default_table_name = table_name or self.pipeline_name
            # TODO: this is not very effective - we consume iterator right away, better implementation needed where we stream iterator to files directly
            all_items: List[DictStrAny] = []
            for item in items:
                # dispatch items by type
                if callable(item):
                    item = item()
                if isinstance(item, dict):
                    all_items.append(item)
                elif isinstance(item, abc.Sequence):
                    all_items.extend(item)
                # react to CTRL-C and shutdowns from controllers
                signals.raise_if_signalled()

            try:
                self._extract_iterator(default_table_name, all_items)
            except Exception:
                raise PipelineStepFailed("extract", self.last_run_exception, runner.LAST_RUN_METRICS)

    def normalize(self, workers: int = 1, max_events_in_chunk: int = 100000) -> int:
        if is_interactive() and workers > 1:
            raise NotImplementedError("Do not use workers in interactive mode ie. in notebook")
        self._verify_normalize_instance()
        # set runtime parameters
        self._normalize_instance.CONFIG.WORKERS = workers
        self._normalize_instance.CONFIG.MAX_EVENTS_IN_CHUNK = max_events_in_chunk
        # switch to thread pool for single worker
        self._normalize_instance.CONFIG.POOL_TYPE = "thread" if workers == 1 else "process"
        try:
            ec = runner.run_pool(self._normalize_instance.CONFIG, self._normalize_instance)
            # in any other case we raise if runner exited with status failed
            if runner.LAST_RUN_METRICS.has_failed:
                raise PipelineStepFailed("normalize", self.last_run_exception, runner.LAST_RUN_METRICS)
            return ec
        except Exception as r_ex:
            # if EXIT_ON_EXCEPTION flag is set, exception will bubble up directly
            raise PipelineStepFailed("normalize", self.last_run_exception, runner.LAST_RUN_METRICS) from r_ex

    def load(self, max_parallel_loads: int = 20) -> int:
        self._verify_loader_instance()
        self._loader_instance.CONFIG.WORKERS = max_parallel_loads
        self._loader_instance.load_client_cls.CONFIG.DEFAULT_SCHEMA_NAME = self.default_schema_name  # type: ignore
        try:
            ec = runner.run_pool(self._loader_instance.CONFIG, self._loader_instance)
            # in any other case we raise if runner exited with status failed
            if runner.LAST_RUN_METRICS.has_failed:
                raise PipelineStepFailed("load", self.last_run_exception, runner.LAST_RUN_METRICS)
            return ec
        except Exception as r_ex:
            # if EXIT_ON_EXCEPTION flag is set, exception will bubble up directly
            raise PipelineStepFailed("load", self.last_run_exception, runner.LAST_RUN_METRICS) from r_ex

    def flush(self) -> None:
        self.normalize()
        self.load()

    @property
    def working_dir(self) -> str:
        return os.path.abspath(self.root_path)

    @property
    def last_run_exception(self) -> BaseException:
        return runner.LAST_RUN_EXCEPTION

    def list_extracted_loads(self) -> Sequence[str]:
        self._verify_loader_instance()
        return self._normalize_instance.normalize_storage.list_files_to_normalize_sorted()

    def list_normalized_loads(self) -> Sequence[str]:
        self._verify_loader_instance()
        return self._loader_instance.load_storage.list_packages()

    def list_completed_loads(self) -> Sequence[str]:
        self._verify_loader_instance()
        return self._loader_instance.load_storage.list_completed_packages()

    def list_failed_jobs(self, load_id: str) -> Sequence[Tuple[str, str]]:
        self._verify_loader_instance()
        failed_jobs: List[Tuple[str, str]] = []
        for file in self._loader_instance.load_storage.list_completed_failed_jobs(load_id):
            if not file.endswith(".exception"):
                try:
                    failed_message = self._loader_instance.load_storage.storage.load(file + ".exception")
                except FileNotFoundError:
                    failed_message = None
                failed_jobs.append((file, failed_message))
        return failed_jobs

    def get_default_schema(self) -> Schema:
        self._verify_normalize_instance()
        return self._normalize_instance.schema_storage.load_schema(self.default_schema_name)

    def set_default_schema(self, new_schema: Schema) -> None:
        if self.default_schema_name:
            # delete old schema
            try:
                self._normalize_instance.schema_storage.remove_schema(self.default_schema_name)
                self.default_schema_name = None
            except FileNotFoundError:
                pass
        # save new schema
        self._normalize_instance.schema_storage.save_schema(new_schema)
        self.default_schema_name = new_schema.name
        with self._managed_state():
            self.state["default_schema_name"] = self.default_schema_name

    def add_schema(self, aux_schema: Schema) -> None:
        self._normalize_instance.schema_storage.save_schema(aux_schema)

    def get_schema(self, name: str) -> Schema:
        return self._normalize_instance.schema_storage.load_schema(name)

    def remove_schema(self, name: str) -> None:
        self._normalize_instance.schema_storage.remove_schema(name)

    def sync_schema(self) -> None:
        self._verify_loader_instance()
        schema = self._normalize_instance.schema_storage.load_schema(self.default_schema_name)
        with self._loader_instance.load_client_cls(schema) as client:
            client.initialize_storage()
            client.update_storage_schema()

    def sql_client(self, schema_name: str = None) -> SqlClientBase[Any]:
        self._verify_loader_instance()
        schema = self._normalize_instance.schema_storage.load_schema(schema_name or self.default_schema_name)
        with self._loader_instance.load_client_cls(schema) as c:
            if isinstance(c, SqlJobClientBase):
                return c.sql_client
            else:
                raise SqlClientNotAvailable(self._loader_instance.CONFIG.CLIENT_TYPE)

    def run_in_pool(self, run_f: Callable[..., Any]) -> int:
        # internal runners should work in single mode
        self._loader_instance.CONFIG.IS_SINGLE_RUN = True
        self._loader_instance.CONFIG.EXIT_ON_EXCEPTION = True
        self._normalize_instance.CONFIG.IS_SINGLE_RUN = True
        self._normalize_instance.CONFIG.EXIT_ON_EXCEPTION = True

        def _run(_: Any) -> TRunMetrics:
            rv = run_f()
            if isinstance(rv, TRunMetrics):
                return rv
            if isinstance(rv, int):
                pending = rv
            else:
                pending = 1
            return TRunMetrics(False, False, int(pending))

        # run the fun
        ec = runner.run_pool(self.C, _run)
        # ec > 0 - signalled
        # -1 - runner was not able to start

        if runner.LAST_RUN_METRICS is not None and runner.LAST_RUN_METRICS.has_failed:
            raise self.last_run_exception
        return ec


    def _configure_normalize(self) -> None:
        # create normalize config
        normalize_initial = {
            "NORMALIZE_VOLUME_PATH": os.path.join(self.root_path, "normalize"),
            "SCHEMA_VOLUME_PATH": os.path.join(self.root_path, "schemas"),
            "EXPORT_SCHEMA_PATH": os.path.abspath(self.export_schema_path) if self.export_schema_path else None,
            "IMPORT_SCHEMA_PATH": os.path.abspath(self.import_schema_path) if self.import_schema_path else None,
            "LOADER_FILE_FORMAT": self._loader_instance.load_client_cls.capabilities()["preferred_loader_file_format"],
            "ADD_EVENT_JSON": False
        }
        normalize_initial.update(self._configure_runner())
        C = normalize_configuration(initial_values=normalize_initial)
        # shares schema storage with the pipeline so we do not need to install
        self._normalize_instance = Normalize(C)

    def _configure_load(self) -> None:
        # use credentials to populate loader client config, it includes also client type
        loader_client_initial = dtc_asdict(self.credentials)
        loader_client_initial["DEFAULT_SCHEMA_NAME"] = self.default_schema_name
        # but client type must be passed to loader config
        loader_initial = {"CLIENT_TYPE": loader_client_initial["CLIENT_TYPE"]}
        loader_initial.update(self._configure_runner())
        loader_initial["DELETE_COMPLETED_JOBS"] = True
        try:
            C = loader_configuration(initial_values=loader_initial)
            self._loader_instance = Load(C, REGISTRY, client_initial_values=loader_client_initial, is_storage_owner=True)
        except ImportError:
            raise MissingDependencyException(
                f"{self.credentials.CLIENT_TYPE} destination",
                [f"python-dlt[{self.credentials.CLIENT_TYPE}]"],
                "Dependencies for specific destination are available as extras of python-dlt"
            )

    def _verify_loader_instance(self) -> None:
        if self._loader_instance is None:
            raise NoPipelineException()

    def _verify_normalize_instance(self) -> None:
        if self._loader_instance is None:
            raise NoPipelineException()

    def _configure_runner(self) -> StrAny:
        return {
            "PIPELINE_NAME": self.pipeline_name,
            "IS_SINGLE_RUN": True,
            "WAIT_RUNS": 0,
            "EXIT_ON_EXCEPTION": True,
            "LOAD_VOLUME_PATH": os.path.join(self.root_path, "normalized")
        }

    def _load_modules(self) -> None:
        # configure loader
        self._configure_load()
        # configure normalize
        self._configure_normalize()

    def _extract_iterator(self, default_table_name: str, items: Sequence[DictStrAny]) -> None:
        try:
            for idx, i in enumerate(items):
                if not isinstance(i, dict):
                    # TODO: convert non dict types into dict
                    items[idx] = i = {"v": i}
                if DLT_METADATA_FIELD not in i or i.get(DLT_METADATA_FIELD, None) is None:
                    # set default table name
                    with_table_name(i, default_table_name)

            load_id = uniq_id()
            self.extractor_storage.save_json(f"{load_id}.json", items)
            self.extractor_storage.commit_events(
                self.default_schema_name,
                self.extractor_storage.storage.make_full_path(f"{load_id}.json"),
                default_table_name,
                len(items),
                load_id
            )

            runner.LAST_RUN_METRICS = TRunMetrics(was_idle=False, has_failed=False, pending_items=0)
        except Exception as ex:
            logger.exception("extracting iterator failed")
            runner.LAST_RUN_METRICS = TRunMetrics(was_idle=False, has_failed=True, pending_items=0)
            runner.LAST_RUN_EXCEPTION = ex
            raise

    @contextmanager
    def _managed_state(self) -> Iterator[None]:
        backup_state = deepcopy(self.state)
        try:
            yield
        except Exception:
            # restore old state
            self.state.clear()
            self.state.update(backup_state)
            raise
        else:
            # persist old state
            self.root_storage.save("state.json", json.dumps(self.state))

    def _restore_state(self) -> None:
        self.state.clear()
        restored_state: DictStrAny = json.loads(self.root_storage.load("state.json"))
        self.state.update(restored_state)

    @staticmethod
    def save_schema_to_file(file_name: str, schema: Schema, remove_defaults: bool = True) -> None:
        with open(file_name, "w", encoding="utf-8") as f:
            f.write(schema.to_pretty_yaml(remove_defaults=remove_defaults))

    @staticmethod
    def load_schema_from_file(file_name: str) -> Schema:
        with open(file_name, "r", encoding="utf-8") as f:
            schema_dict: DictStrAny = yaml.safe_load(f)
        return Schema.from_dict(schema_dict)