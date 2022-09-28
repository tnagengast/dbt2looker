import logging
import json
import jsonschema
import importlib.resources
from typing import Dict, Optional, List, Union
from functools import reduce

from .generator import _extract_all_refs
from . import models


def validate_manifest(raw_manifest: dict):
    with importlib.resources.open_text("yoda_dbt2looker.dbt_json_schemas", "manifest_dbt2looker.json") as f:
        schema = json.load(f)
    v = jsonschema.Draft7Validator(schema)
    hasError = False
    for error in v.iter_errors(raw_manifest):
        raise_error_context(error)
        hasError = True
    if hasError:
        raise ValueError("Failed to parse dbt manifest.json")
    return True


def raise_error_context(error: jsonschema.ValidationError, offset=''):
    for error in sorted(error.context, key=lambda e: e.schema_path):
        raise_error_context(error, offset=offset + '  ')
    path = '.'.join([str(p) for p in error.absolute_path])
    logging.error(f'{offset}Error in manifest at {path}: {error.message}')


def validate_catalog(raw_catalog: dict):
    return True


def parse_dbt_project_config(raw_config: dict):
    return models.DbtProjectConfig(**raw_config)


def parse_catalog_nodes(raw_catalog: dict):
    catalog = models.DbtCatalog(**raw_catalog)
    return catalog.nodes


def parse_adapter_type(raw_manifest: dict):
    manifest = models.DbtManifest(**raw_manifest)
    return manifest.metadata.adapter_type


def tags_match(query_tag: str, model: models.DbtModel) -> bool:
    try:
        return query_tag in model.tags
    except AttributeError:
        return False
    except ValueError:
        # Is the tag just a string?
        return query_tag == model.tags


def parse_models(raw_manifest: dict, tag=None) -> List[models.DbtModel]:
    manifest = models.DbtManifest(**raw_manifest)
    all_models: List[models.DbtModel] = [
        node
        for node in manifest.nodes.values()
        if node.resource_type == 'model'
    ]

    # Empty model files have many missing parameters
    for model in all_models:
        if not hasattr(model, 'name'):
            logging.error('Cannot parse model with id: "%s" - is the model file empty?', model.unique_id)
            raise SystemExit('Failed')

    if tag is None:
        return all_models
    return [model for model in all_models if tags_match(tag, model)]


def parse_exposures(raw_manifest: dict, tag=None) -> List[models.DbtExposure]:
    manifest = models.DbtManifest(**raw_manifest)
    # Empty model files have many missing parameters
    all_exposures = manifest.exposures.values()
    for exposure in all_exposures:
        if not hasattr(exposure, 'name'):
            logging.error('Cannot parse exposure with id: "%s" - is the exposure file empty?', exposure.unique_id)
            raise SystemExit('Failed')

    if tag is None:
        return all_exposures
    return [exposure for exposure in all_exposures if tags_match(tag, exposure)]


def check_models_for_missing_column_types(dbt_typed_models: List[models.DbtModel]):
    for model in dbt_typed_models:
        if all([col.data_type is None for col in model.columns.values()]):
            logging.debug('Model %s has no typed columns, no dimensions will be generated. %s', model.unique_id, model)


def parse_typed_models(raw_manifest: dict, raw_catalog: dict, dbt_project_name: str, tag: Optional[str] = None):
    catalog_nodes = parse_catalog_nodes(raw_catalog)
    dbt_models = parse_models(raw_manifest, tag=tag)
    manifest = models.DbtManifest(**raw_manifest)
    typed_dbt_exposures: List[models.DbtExposure] = parse_exposures(raw_manifest, tag=tag)
    exposure_nodes = []  # [manifest.nodes.get(mode_name) for exposure in typed_dbt_exposures for mode_name in exposure.depends_on.nodes]

    exposure_model_views = set()
    for exposure in typed_dbt_exposures:
        ref_model = _extract_all_refs(exposure.meta.looker.main_model)
        if not ref_model:
            logging.error(f"Exposure main_model {exposure.meta.looker.main_model} should be ref('model_name')")
            raise Exception(f"Exposure main_model {exposure.meta.looker.main_model} should be ref('model_name')")
        exposure_model_views.add(ref_model[0])

        if exposure.meta.looker.joins:
            for join in exposure.meta.looker.joins:
                if _extract_all_refs(join.sql_on) == None:
                    logging.error(f"Exposure join.sql_on {join.sql_on} should be ref('model_name')")
                    raise Exception(f"Exposure join.sql_on {join.sql_on} should be ref('model_name')")

            for item in reduce(list.__add__, [_extract_all_refs(join.sql_on) for join in exposure.meta.looker.joins]):
                exposure_model_views.add(item)

    for model in exposure_model_views:
        model_loopup = f"model.{dbt_project_name}.{model}"
        model_node = manifest.nodes.get(model_loopup)
        if not model_node:
            logging.error(f"Exposure join.sql_on model {model_loopup} missing")
            raise Exception(f"Exposure join.sql_on model {model_loopup} missing")
        model_node.create_explorer = False
        exposure_nodes.append(model_node)

    adapter_type = parse_adapter_type(raw_manifest)
    dbt_models = dbt_models + exposure_nodes
    logging.debug('Parsed %d models from manifest.json', len(dbt_models))
    for model in dbt_models:
        logging.debug(
            'Model %s has %d columns with %d measures',
            model.name,
            len(model.columns),
            reduce(lambda acc, col: acc + len(col.meta.measures) + len(col.meta.measure) + len(col.meta.metrics) + len(col.meta.metric), model.columns.values(), 0)
        )

    # Check catalog for models
    for model in dbt_models:
        if model.unique_id not in catalog_nodes:
            logging.warning(
                f'Model {model.unique_id} not found in catalog. No looker view will be generated. '
                f'Check if model has materialized in {adapter_type} at {model.relation_name}')

    # Update dbt models with data types from catalog
    dbt_typed_models = [
        model.copy(update={'columns': {
            column.name: column.copy(update={
                'data_type': get_column_type_from_catalog(catalog_nodes, model.unique_id, column.name)
            })
            for column in model.columns.values()
        }})
        for model in dbt_models
        if model.unique_id in catalog_nodes
    ]
    logging.debug('Found catalog entries for %d models', len(dbt_typed_models))
    logging.debug('Catalog entries missing for %d models', len(dbt_models) - len(dbt_typed_models))
    check_models_for_missing_column_types(dbt_typed_models)
    return dbt_typed_models


def get_column_type_from_catalog(catalog_nodes: Dict[str, models.DbtCatalogNode], model_id: str, column_name: str):
    node = catalog_nodes.get(model_id)
    column = None if node is None else node.columns.get(column_name)
    return None if column is None else column.type
