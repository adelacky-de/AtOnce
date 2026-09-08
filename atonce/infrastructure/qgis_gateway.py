"""Compatibility facade for the focused QGIS infrastructure runtimes."""

from typing import Dict, List, Optional, Tuple

from qgis.core import QgsProject

from ..core.lineage import SourceKeyPlan
from ..core.propagation import DerivedSchemaPlan, FieldSpec
from ..models.workflow import LayerRef
from .delivery_runtime import KmlStageResult, ShapefileStageResult, XlsxStageResult, DeliveryRuntime
from .derived_materialization_runtime import DerivedMaterializationRuntime
from .freeform_operation_runtime import FreeformOperationRuntime
from .qgis_layer_runtime import QgisLayerRuntime
from .raster_operation_runtime import RasterOperationRuntime
from .source_identity_runtime import SourceIdentityRuntime, SourceKeyPreflight
from .spatial_operation_runtime import SpatialOperationRuntime


class QgisGateway:
    """Stable public boundary over focused QGIS runtime modules."""

    def __init__(self, project: Optional[QgsProject] = None):
        self.project = project or QgsProject.instance()
        self.layers = QgisLayerRuntime(self.project)
        self.source_identity = SourceIdentityRuntime(self.layers)
        self.freeform = FreeformOperationRuntime(self.project, self.layers, self.source_identity)
        self.spatial = SpatialOperationRuntime(self.project, self.layers, self.source_identity)
        self.raster = RasterOperationRuntime(self.project, self.layers)
        self.materialization = DerivedMaterializationRuntime(self.project, self.layers, self.source_identity)
        self.delivery = DeliveryRuntime(self.project, self.layers)

    def resolve_layer(self, layer_id):
        return self.layers.resolve_layer(layer_id)

    def vector_layers(self) -> List[object]:
        return self.layers.vector_layers()

    def raster_layers(self) -> List[object]:
        return self.layers.raster_layers()

    def source_candidate_layers(self) -> List[object]:
        return self.layers.source_candidate_layers()

    @staticmethod
    def make_layer_ref(layer, role: str) -> LayerRef:
        return QgisLayerRuntime.make_layer_ref(layer, role)

    @staticmethod
    def make_source_layer_ref(layer) -> LayerRef:
        return QgisLayerRuntime.make_source_layer_ref(layer)

    @staticmethod
    def is_vector_layer(layer) -> bool:
        return QgisLayerRuntime.is_vector_layer(layer)

    @staticmethod
    def is_raster_layer(layer) -> bool:
        return QgisLayerRuntime.is_raster_layer(layer)

    @classmethod
    def layer_data_type(cls, layer) -> str:
        return QgisLayerRuntime.layer_data_type(layer)

    @staticmethod
    def field_names(layer) -> List[str]:
        return QgisLayerRuntime.field_names(layer)

    @classmethod
    def has_field(cls, layer, field_name: str) -> bool:
        return bool(field_name) and field_name in cls.field_names(layer)

    @staticmethod
    def field_specs(layer) -> List[FieldSpec]:
        return QgisLayerRuntime.field_specs(layer)

    @staticmethod
    def validate_expression(expression_text: str, layer=None) -> Tuple[bool, str]:
        return QgisLayerRuntime.validate_expression(expression_text, layer)

    @staticmethod
    def validate_crs(authid: str) -> Tuple[bool, str]:
        return QgisLayerRuntime.validate_crs(authid)

    @staticmethod
    def require_equivalent_crs(layers, operation: str) -> None:
        return QgisLayerRuntime.require_equivalent_crs(layers, operation)

    def preflight_source_keys(self, layer) -> SourceKeyPreflight:
        return self.source_identity.preflight_source_keys(layer)

    def preflight_existing_source_keys(self, layer) -> Dict[int, str]:
        return self.source_identity.preflight_existing_source_keys(layer)

    def build_filtered_memory_layer(self, workflow_id: str, node_id: str, display_name: str, source_layer, source_lineage_id: str, expression: str):
        return self.freeform.build_filtered_memory_layer(workflow_id, node_id, display_name, source_layer, source_lineage_id, expression)

    def build_passthrough_memory_layer(self, workflow_id: str, node_id: str, display_name: str, source_layer, source_lineage_id: str=''):
        return self.freeform.build_passthrough_memory_layer(workflow_id, node_id, display_name, source_layer, source_lineage_id)

    def build_field_mapping_memory_layer(self, workflow_id, node_id, display_name, parent_layer, source_lineage_id, parameters):
        return self.freeform.build_field_mapping_memory_layer(workflow_id, node_id, display_name, parent_layer, source_lineage_id, parameters)

    def build_keep_fields_memory_layer(self, workflow_id, node_id, display_name, parent_layer, source_lineage_id, parameters):
        return self.freeform.build_keep_fields_memory_layer(workflow_id, node_id, display_name, parent_layer, source_lineage_id, parameters)

    def build_rename_field_memory_layer(self, workflow_id, node_id, display_name, parent_layer, source_lineage_id, parameters):
        return self.freeform.build_rename_field_memory_layer(workflow_id, node_id, display_name, parent_layer, source_lineage_id, parameters)

    def build_change_field_type_memory_layer(self, workflow_id, node_id, display_name, parent_layer, source_lineage_id, parameters):
        return self.freeform.build_change_field_type_memory_layer(workflow_id, node_id, display_name, parent_layer, source_lineage_id, parameters)

    def build_sort_memory_layer(self, workflow_id, node_id, display_name, parent_layer, source_lineage_id, parameters):
        return self.freeform.build_sort_memory_layer(workflow_id, node_id, display_name, parent_layer, source_lineage_id, parameters)

    def build_aggregate_memory_layer(self, workflow_id, node_id, display_name, parent_layer, source_lineage_id, parameters):
        return self.freeform.build_aggregate_memory_layer(workflow_id, node_id, display_name, parent_layer, source_lineage_id, parameters)

    def build_compare_changes_memory_layer(self, workflow_id, node_id, display_name, parent_layers, parent_lineage_ids, parameters):
        return self.freeform.build_compare_changes_memory_layer(workflow_id, node_id, display_name, parent_layers, parent_lineage_ids, parameters)

    def build_join_memory_layer(self, workflow_id, node_id, display_name, parent_layers, parent_lineage_ids, parameters):
        return self.freeform.build_join_memory_layer(workflow_id, node_id, display_name, parent_layers, parent_lineage_ids, parameters)

    def build_calculate_field_memory_layer(self, workflow_id, node_id, display_name, parent_layer, source_lineage_id, parameters):
        return self.freeform.build_calculate_field_memory_layer(workflow_id, node_id, display_name, parent_layer, source_lineage_id, parameters)

    def build_remove_duplicates_memory_layer(self, workflow_id, node_id, display_name, parent_layer, source_lineage_id, parameters):
        return self.freeform.build_remove_duplicates_memory_layer(workflow_id, node_id, display_name, parent_layer, source_lineage_id, parameters)

    def build_buffer_memory_layer(self, workflow_id, node_id, display_name, parent_layer, source_lineage_id, parameters):
        return self.spatial.build_buffer_memory_layer(workflow_id, node_id, display_name, parent_layer, source_lineage_id, parameters)

    def build_reproject_memory_layer(self, workflow_id, node_id, display_name, parent_layer, source_lineage_id, parameters):
        return self.spatial.build_reproject_memory_layer(workflow_id, node_id, display_name, parent_layer, source_lineage_id, parameters)

    def build_select_by_location_memory_layer(self, workflow_id, node_id, display_name, parent_layers, parent_lineage_ids, parameters):
        return self.spatial.build_select_by_location_memory_layer(workflow_id, node_id, display_name, parent_layers, parent_lineage_ids, parameters)

    def build_spatial_join_memory_layer(self, workflow_id, node_id, display_name, parent_layers, parent_lineage_ids, parameters):
        return self.spatial.build_spatial_join_memory_layer(workflow_id, node_id, display_name, parent_layers, parent_lineage_ids, parameters)

    def build_clip_memory_layer(self, workflow_id, node_id, display_name, parent_layers, parent_lineage_ids, parameters):
        return self.spatial.build_clip_memory_layer(workflow_id, node_id, display_name, parent_layers, parent_lineage_ids, parameters)

    def build_dissolve_memory_layer(self, workflow_id, node_id, display_name, parent_layer, source_lineage_id, parameters):
        return self.spatial.build_dissolve_memory_layer(workflow_id, node_id, display_name, parent_layer, source_lineage_id, parameters)

    def preflight_merge_layers(self, parent_layers):
        return self.materialization.preflight_merge_layers(parent_layers)

    def build_merged_memory_layer(self, workflow_id: str, node_id: str, display_name: str, parent_layers, parent_lineage_ids):
        return self.materialization.build_merged_memory_layer(workflow_id, node_id, display_name, parent_layers, parent_lineage_ids)

    def apply_source_key_plan(self, layer, preflight: SourceKeyPreflight) -> int:
        return self.source_identity.apply_source_key_plan(layer, preflight)

    def preflight_derived_schema(self, left_layer, right_layer, mapping) -> DerivedSchemaPlan:
        return self.materialization.preflight_derived_schema(left_layer, right_layer, mapping)

    def build_derived_memory_layer(self, workflow_id: str, name: str, left_layer, right_layer, schema_plan: DerivedSchemaPlan, source_key_plans: Dict[str, SourceKeyPlan]):
        return self.materialization.build_derived_memory_layer(workflow_id, name, left_layer, right_layer, schema_plan, source_key_plans)

    def replace_project_derived_layer(self, old_layer_id: Optional[str], new_layer):
        return self.materialization.replace_project_derived_layer(old_layer_id, new_layer)

    def replace_graph_node_layer(self, old_layer_id: Optional[str], new_layer):
        return self.materialization.replace_graph_node_layer(old_layer_id, new_layer)

    def restore_graph_node_layer(self, old_layer, new_layer_id: Optional[str]=None):
        return self.materialization.restore_graph_node_layer(old_layer, new_layer_id)

    def require_exportable_lineage(self, layer) -> None:
        return self.delivery.require_exportable_lineage(layer)

    def stage_geopackage(self, layer, staged_path: str, layer_name: str='layer_c') -> int:
        return self.delivery.stage_geopackage(layer, staged_path, layer_name)

    def stage_geojson(self, layer, staged_path: str) -> int:
        return self.delivery.stage_geojson(layer, staged_path)

    def validate_shapefile_schema(self, layer) -> None:
        return self.delivery.validate_shapefile_schema(layer)

    def stage_shapefile(self, layer, staged_shp_path: str) -> ShapefileStageResult:
        return self.delivery.stage_shapefile(layer, staged_shp_path)

    def stage_kml(self, layer, staged_path: str) -> KmlStageResult:
        return self.delivery.stage_kml(layer, staged_path)

    def stage_kmz(self, layer, staged_kmz_path: str):
        return self.delivery.stage_kmz(layer, staged_kmz_path)

    def build_reprojected_raster_layer(
        self, workflow_id, node_id, display_name, source_layer, parameters
    ):
        return self.raster.build_reprojected_raster_layer(
            workflow_id, node_id, display_name, source_layer, parameters
        )

    def build_converted_raster_layer(
        self, workflow_id, node_id, display_name, source_layer, parameters
    ):
        return self.raster.build_converted_raster_layer(
            workflow_id, node_id, display_name, source_layer, parameters
        )

    def stage_geotiff(self, layer, staged_path: str) -> int:
        return self.raster.stage_geotiff(layer, staged_path)

    def stage_raster_kmz(
        self, layer, staged_path: str, *, display_name: str = ""
    ) -> int:
        return self.raster.stage_raster_kmz(
            layer, staged_path, display_name=display_name
        )

    def stage_xlsx(self, layer, staged_path: str, export_id: str, revision_number: int, filter_expression: str='') -> XlsxStageResult:
        return self.delivery.stage_xlsx(layer, staged_path, export_id, revision_number, filter_expression)

    def resolve_feature(self, layer_id, feature_key):
        raise NotImplementedError

    def apply_attribute_changes(self, changes):
        raise NotImplementedError
