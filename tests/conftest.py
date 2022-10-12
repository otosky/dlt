import os
import dataclasses

def pytest_configure(config):
    # patch the configurations to use test storage by default, we modify the types (classes) fields
    # the dataclass implementation will use those patched values when creating instances (the values present
    # in the declaration are not frozen allowing patching)

    from dlt.common.configuration.specs import normalize_volume_configuration, run_configuration, load_volume_configuration, schema_volume_configuration

    test_storage_root = "_storage"
    run_configuration.RunConfiguration.config_files_storage_path = os.path.join(test_storage_root, "config/%s")

    load_volume_configuration.LoadVolumeConfiguration.load_volume_path = os.path.join(test_storage_root, "load")

    normalize_volume_configuration.NormalizeVolumeConfiguration.normalize_volume_path = os.path.join(test_storage_root, "normalize")
    if hasattr(normalize_volume_configuration.NormalizeVolumeConfiguration, "__init__"):
        # delete __init__, otherwise it will not be recreated by dataclass
        delattr(normalize_volume_configuration.NormalizeVolumeConfiguration, "__init__")
        normalize_volume_configuration.NormalizeVolumeConfiguration = dataclasses.dataclass(normalize_volume_configuration.NormalizeVolumeConfiguration, init=True, repr=False)

    schema_volume_configuration.SchemaVolumeConfiguration.schema_volume_path = os.path.join(test_storage_root, "schemas")

    assert run_configuration.RunConfiguration.config_files_storage_path == os.path.join(test_storage_root, "config/%s")
    assert run_configuration.RunConfiguration().config_files_storage_path == os.path.join(test_storage_root, "config/%s")
