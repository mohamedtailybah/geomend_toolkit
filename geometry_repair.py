import os
from qgis.PyQt.QtWidgets import QAction,QMessageBox,QApplication
from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsUnitTypes,QgsProject,QgsVectorLayer,QgsFeature,QgsWkbTypes,QgsFeedback,QgsField,QgsGeometry
from qgis.PyQt.QtCore import QVariant
from .geometry_repair_dialog import GeometryRepairDialog, PLUGIN_TITLE
from . import geometry_tools


class GeometryRepairPlugin:
    def __init__(self,iface):
        self.iface=iface; self.plugin_dir=os.path.dirname(__file__); self.action=None; self.dialog=None

    def initGui(self):
        icon_path=os.path.join(self.plugin_dir,'logo.png')
        self.action=QAction(QIcon(icon_path) if os.path.exists(icon_path) else QIcon(),PLUGIN_TITLE,self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu('&'+PLUGIN_TITLE,self.action)

    def unload(self):
        if self.dialog is not None:
            self.dialog.close()
        if self.action:
            self.iface.removePluginMenu('&'+PLUGIN_TITLE,self.action)
            self.iface.removeToolBarIcon(self.action)

    def run(self):
        # The plugin restarts from scratch every time it is opened: the dialog
        # is discarded when closed (see _on_dialog_closed), so a fresh one with
        # default settings, empty reports and a clean Run button is built here.
        if self.dialog is None:
            self.dialog=GeometryRepairDialog(self.iface.mainWindow())
            self.dialog.run_btn.clicked.connect(self.run_active_tab)
            self.dialog.finished.connect(self._on_dialog_closed)
            self.dialog.refresh_layer_search()
        self._sync_active_layer()
        self.dialog.show(); self.dialog.raise_(); self.dialog.activateWindow()

    def _on_dialog_closed(self,*_):
        d=self.dialog
        self.dialog=None
        if d is not None:
            d.deleteLater()

    def _sync_active_layer(self):
        """Whenever the plugin is opened, default the Layer selector to
        whichever layer is currently selected/active in the QGIS Layers
        panel, if it's a vector layer the combo can offer."""
        active=self.iface.activeLayer()
        if isinstance(active,QgsVectorLayer):
            self.dialog.select_active_layer(active)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    def run_active_tab(self):
        tab=self.dialog.current_tab_name()
        if tab=='Help':
            return
        # 100% Success Indicator: every Run click starts from a clean 0%, only
        # ever reaches 100% on genuine successful completion, and on any
        # unexpected failure the bar is stopped (reset) with an error message
        # instead of silently showing a false 100%.
        if not self._check_output_destination():
            return
        self.dialog.set_progress(0)
        self.dialog.run_btn.setEnabled(False)
        QApplication.processEvents()
        try:
            if tab=='Inspect and Repair': self.run_inspect_repair()
            elif tab=='Inward Buffer': self.run_inward_buffers()
            elif tab=='Line Layer Buffer': self.run_line_buffers()
            elif tab=='Advance Digitization': self.run_advance_digitization()
        except Exception as e:
            self.dialog.set_progress(0)
            QMessageBox.critical(self.dialog,'Operation failed',
                f'The operation did not complete successfully and was stopped:\n\n{e}')
            return
        finally:
            self.dialog.run_btn.setEnabled(True)
        if self.dialog.progress.value()==100:
            self.dialog.mark_run_completed()

    def _make_stage_feedback(self):
        """A QgsFeedback whose 0-100 setProgress() calls are rescaled into a
        [start,end] window on the dialog's progress bar via _set_stage(), so
        the Run button's progress indicator visibly advances through each
        stage of the operation toward 100%, rather than jumping straight
        from 0 to 100."""
        feedback=QgsFeedback()
        self._stage_range=(0,100)
        def _on_progress(value):
            start,end=self._stage_range
            scaled=start+(max(0.0,min(100.0,value))/100.0)*(end-start)
            self.dialog.set_progress(scaled)
            QApplication.processEvents()
        feedback.progressChanged.connect(_on_progress)
        return feedback

    def _set_stage(self,start,end):
        self._stage_range=(start,end)
        self.dialog.set_progress(start)
        QApplication.processEvents()

    def _validate_layer_selected(self,layer):
        if layer is None or not isinstance(layer,QgsVectorLayer):
            QMessageBox.warning(self.dialog,'No vector layer','Please choose a vector layer.'); return False
        return True

    def _check_output_destination(self):
        """Fail fast, before doing any work, if a folder was chosen (making
        outputs permanent) but that folder is no longer there."""
        if self.dialog.output_to_disk() and not os.path.isdir(self.dialog.output_folder()):
            QMessageBox.warning(self.dialog,'Output folder not found',
                'Output Destination has a folder set, but it no longer exists or '
                'is not accessible:\n\n'+self.dialog.output_folder()+'\n\n'
                'Click "Browse…" to pick a folder again, or "Clear" to keep new '
                'layers temporary.')
            return False
        return True

    def _finalize_layer(self,layer):
        """Add a freshly built output layer to the project, honouring the
        user's Output Destination choice: either as a temporary (memory,
        not saved to disk) layer -- the default when no folder is set -- or
        written to disk, in the chosen file type, in the chosen folder, and
        reopened from there. Returns the layer that actually ends up in the
        project (the original memory layer, or the reloaded disk layer),
        since later code may still read its name/extent/feature count."""
        if self.dialog.output_to_disk():
            layer=geometry_tools.save_layer_to_disk(layer,self.dialog.output_folder(),self.dialog.output_file_driver())
        QgsProject.instance().addMapLayer(layer)
        return layer

    # ------------------------------------------------------------------
    # Tab 1: Inspect and Repair
    # ------------------------------------------------------------------
    def run_inspect_repair(self):
        layer=self.dialog.selected_layer()
        if not self._validate_layer_selected(layer): return
        feature_ids=self.dialog.processing_feature_ids(layer)
        if feature_ids is not None and not feature_ids:
            QMessageBox.warning(self.dialog,'No selected features','Select at least one feature in the input layer, or untick “Selected features only”.')
            return
        feedback=self._make_stage_feedback()
        is_polygon=QgsWkbTypes.geometryType(layer.wkbType())==QgsWkbTypes.PolygonGeometry

        self._set_stage(0,10)
        invalid=geometry_tools.check_validity(layer,feedback) if self.dialog.fix_invalid_checked() else []
        self._set_stage(10,20)
        gap_tol=0.0
        if self.dialog.find_gaps_checked() and is_polygon:
            try:
                gap_tol=geometry_tools.convert_distance(self.dialog.gap_tolerance_value(),self.dialog.gap_tolerance_unit(),layer.crs())
            except ValueError as e:
                self.dialog.set_progress(0)
                QMessageBox.warning(self.dialog,'Gap tolerance / CRS problem',str(e)); return
        allow_sliver=self.dialog.gap_sliver_checked()
        gaps=geometry_tools.find_gaps(layer,feedback=feedback,feature_ids=feature_ids,tolerance=gap_tol,allow_slivers=allow_sliver) if gap_tol>0 else []
        self._set_stage(20,30)
        duplicates=geometry_tools.find_duplicate_geometries(layer,feedback=feedback,feature_ids=feature_ids) if self.dialog.remove_duplicates_checked() else []

        # Work from repaired geometries for overlap detection so invalid polygon
        # topology cannot make the exact overlap layer empty.
        geometries={f.id():QgsGeometry(f.geometry()) for f in layer.getFeatures() if feature_ids is None or f.id() in feature_ids}
        original_geometries=dict(geometries)
        snapped=False
        # Unselected features (Selected-features-only mode) are NOT written to
        # the output and are never modified, but they still bound gaps, so they
        # are kept as immutable context for gap detection / verification.
        context_geometries={}
        if feature_ids is not None:
            for f in layer.getFeatures():
                if f.id() in feature_ids: continue
                cg=f.geometry()
                if cg is None or cg.isEmpty(): continue
                cg=QgsGeometry(cg)
                if not cg.isGeosValid():
                    cg,_,_=geometry_tools.fix_invalid_geometry(cg)
                context_geometries[f.id()]=cg
        if self.dialog.fix_invalid_checked():
            fixed=unresolved=0
            for fid,g in list(geometries.items()):
                ng,ch,bad=geometry_tools.fix_invalid_geometry(g)
                geometries[fid]=ng
                fixed+=int(ch); unresolved+=int(bad)
        else:
            fixed=unresolved=0

        # Gap correction is deliberately performed AFTER overlap removal.
        # Doing it earlier can have the overlap-cut stage reopen a gap along
        # the newly created shared boundary.  The original `gaps` list above
        # is retained for reporting/inspection; the actual correction later
        # is calculated from the current working geometries.
        gaps_corrected=0
        gap_details=[]

        # --------------------------------------------------------------
        # Overlap Section: produces exactly two output layers —
        #   1) Overlap Areas          — the exact overlap/intersection geometry
        #   2) Remaining Polygons Without Overlap — the refined, overlap-free
        #      version of the input polygons (never the raw originals; no
        #      third "Problem Areas" layer is ever created here).
        # --------------------------------------------------------------
        self._set_stage(30,50)
        overlaps=geometry_tools.find_overlaps(layer,geometries=geometries,feedback=feedback,feature_ids=feature_ids) if (self.dialog.fix_overlaps_checked() and is_polygon) else []
        overlap_layer=None
        if overlaps:
            overlap_layer=self._make_overlap_layer(layer,overlaps,self.dialog.overlap_layer_name())
        else:
            self._remove_layers_named(self.dialog.overlap_layer_name())

        self._set_stage(50,55)
        # Small gaps are corrected directly in the final remaining-polygons
        # geometry. No separate gap layer is created: the output layer itself
        # is the authoritative corrected result.
        checks_on_polygon=is_polygon
        report=[f'Checked layer: {layer.name()}',f'Feature count: {layer.featureCount()}','']
        if self.dialog.fix_invalid_checked():
            report.append(f'Invalid geometry: {len(invalid)} problem(s) found' if invalid else 'Invalid geometry: none found')
        if self.dialog.fix_overlaps_checked():
            if not is_polygon: report.append('Overlap: not applicable (not a polygon layer) — no layer created')
            elif overlaps: report.append(f'Overlap: {len(overlaps)} overlapping area(s) found')
            else: report.append('Overlap: none found — no overlap layer created')
        if self.dialog.find_gaps_checked():
            if not is_polygon: report.append('Gap: not applicable (not a polygon layer) — no layer created')
            elif gap_tol<=0: report.append('Gap: skipped — Gap tolerance is 0. Enter a tolerance greater than 0 to check and correct gaps — no layer created')
            elif gaps: report.append(f'Gap: {len(gaps)} gap(s) found')
            else: report.append('Gap: none found — no gap layer created')
        if self.dialog.remove_duplicates_checked():
            report.append(f'Duplicate: {len(duplicates)} duplicate feature(s) found' if duplicates else 'Duplicate: none found')

        if self.dialog.zoom_checked() and overlap_layer and overlap_layer.featureCount():
            self.iface.mapCanvas().setExtent(overlap_layer.extent()); self.iface.mapCanvas().refresh()

        if self.dialog.report_only_checked():
            if overlap_layer: report.append(f'Created: {overlap_layer.name()} ({overlap_layer.featureCount()} overlap feature(s))')
            if gap_tol>0 and gaps:
                gl=self._make_gap_layer(layer,gaps,self.dialog.gap_layer_name())
                report.append(f'Created: {gl.name()} ({gl.featureCount()} gap(s), none corrected — report only)')
            else:
                self._remove_layers_named(self.dialog.gap_layer_name())
            report.append(''); report.append('Operation completed successfully — 100%')
            self.dialog.set_report('\n'.join(report)); self.dialog.set_progress(100); return

        if self.dialog.fix_invalid_checked():
            report += [f'Invalid geometries repaired: {fixed}',f'Invalid geometries still unresolved: {unresolved}']

        self._set_stage(55,60)
        if self.dialog.snap_checked():
            under=geometry_tools.polygon_layers_underneath(layer)
            try:
                tol=geometry_tools.convert_distance(self.dialog.snap_distance_value(),self.dialog.snap_unit_value(),layer.crs())
            except ValueError as e:
                self.dialog.set_progress(0)
                QMessageBox.warning(self.dialog,'Distance / CRS problem',str(e)); return
            geometries,used,msg=geometry_tools.snap_layer_to_underlying(layer,geometries,tol,under)
            snapped=bool(used)
            report.append('Snapping: '+('applied to underlying polygon layers.' if used else 'not applied — '+msg))

        if self.dialog.fix_overlaps_checked() and is_polygon:
            # The second Overlap Section output must be a true geometrically
            # refined result: every overlapping portion is subtracted from the
            # appropriate polygon (later feature keeps the shared area, earlier
            # feature is cut), so the layer that comes out of this step has
            # zero polygon-on-polygon overlap anywhere, for one overlap or many.
            self._set_stage(60,85)
            geometries,changed,removed_area=geometry_tools.remove_overlaps_within_layer(geometries,feedback)
            report += [f'Overlap removed — features trimmed: {changed}',
                       f'Area cut to remove overlap: {removed_area:g}',
                       'Priority: later feature keeps the overlap; earlier feature is trimmed.']
            if self.dialog.cross_layer_overlap_checked():
                under=geometry_tools.polygon_layers_underneath(layer)
                self._set_stage(85,92)
                geometries,changed2,removed_area2=geometry_tools.cut_overlap_with_underlying(layer,geometries,under,feedback)
                report += [f'Also cut against {len(under)} layer(s) below in the Layers panel — features trimmed: {changed2}, area cut: {removed_area2:g}']

        # Duplicates are removed BEFORE gap correction so a gap can never be
        # assigned to a duplicate that is then deleted (which would re-open it).
        if self.dialog.remove_duplicates_checked() and duplicates:
            dup_fids={d['fid'] for d in duplicates}
            geometries=geometry_tools.remove_duplicates(geometries,dup_fids)
            report.append(f'Duplicate features removed from output: {len(dup_fids)}')

        # Layer 3: polygons without overlap (before any gap is filled).
        # Only created when something was actually repaired at this stage.
        repaired_stage=bool(overlaps) or (self.dialog.fix_invalid_checked() and fixed>0) \
            or (self.dialog.remove_duplicates_checked() and bool(duplicates)) or snapped
        no_overlap_layer=None
        if repaired_stage:
            no_overlap_layer=self._finalize_layer(self._build_output_layer(
                layer,geometries,self.dialog.output_layer_name(),original_geometries))
        else:
            self._remove_layers_named(self.dialog.output_layer_name(),'GeoMend_Remaining_Polygons_Without_Overlap')

        # --------------------------------------------------------------
        # Final small-gap elimination (after ALL overlap / duplicate work)
        # --------------------------------------------------------------
        # The actual gap geometry is detected, assigned to one neighbouring
        # editable polygon (longest shared boundary, lowest FID on ties),
        # unioned into it and verified.  Iterates until nothing more can be
        # corrected.  Legitimate single-polygon holes are never filled.
        gap_result=None; added_by_fid={}
        if gap_tol>0:
            self._set_stage(92,96)
            gap_result=geometry_tools.eliminate_small_gaps(
                geometries,gap_tol,allowed_fids=set(geometries.keys()),
                context_geometries=context_geometries,feedback=feedback,allow_slivers=allow_sliver)
            geometries=gap_result['geometries']
            added_by_fid=gap_result['added_area']
            n_fixed=gap_result['corrected_count']
            left=[g for g in gap_result['gaps'] if not g['corrected']]
            report += ['',f'Gap tolerance: {self.dialog.gap_tolerance_value():g} {self.dialog.gap_tolerance_unit()} (max gap width)',
                       f'Gaps eliminated (verified, incorporated into neighbouring polygon): {n_fixed}',
                       f'Gaps retained / not corrected: {len(left)}',
                       f'Correction passes used: {gap_result["iterations"]} (max {geometry_tools.GAP_MAX_ITERATIONS})']
            if gap_result['max_iterations_reached']:
                report.append(f'WARNING: maximum of {geometry_tools.GAP_MAX_ITERATIONS} passes reached — some qualifying gaps may remain.')
            recs=gap_result['gaps']
            if recs:
                report.append('gap_id | area | width | neighbor_fids | corrected | target_fid | note')
                for g in recs[:60]:
                    report.append(f"{g['gap_id']} | {g['area']:.6g} | {g['width']:.4g} | {g['neighbor_fids']} | {g['corrected']} | {g['target_fid']} | {g['reason']}")
                if len(recs)>60: report.append(f'… {len(recs)-60} more gap record(s) not shown')

        # --------------------------------------------------------------
        # Final topology verification on the real final geometries
        # --------------------------------------------------------------
        if gap_tol>0 or self.dialog.fix_overlaps_checked():
            check_tol=gap_tol
            if check_tol<=0:
                # Overlap/validity check only; a nominal tolerance keeps the numeric noise threshold sensible.
                try: check_tol=geometry_tools.convert_distance(0.5,'Meters',layer.crs())
                except ValueError: check_tol=1e-6
            v=geometry_tools.verify_topology(geometries,check_tol if gap_tol>0 else 0.0,noise_tol=check_tol,context_geometries=context_geometries,
                                             allowed_fids=set(geometries.keys()),allow_slivers=allow_sliver)
            report += ['','Final topology check:',
                       f"  Remaining qualifying gaps: {v['remaining_qualifying_gaps']}" if gap_tol>0 else '  Gap check: not enabled',
                       *([f"  Wider gaps retained (not modified): {v['retained_wider_gaps']}"] if gap_tol>0 else []),
                       f"  Positive-area overlaps: {v['overlap_count']} (area {v['overlap_area']:g})",
                       f"  Invalid geometries: {v['invalid_geometries']}"]
            if v['overlaps_with_unselected']:
                report.append(f"  Note: {v['overlaps_with_unselected']} overlap(s) involve unselected features (never modified).")
            if gap_tol>0 and v['clean'] and not (gap_result and gap_result['max_iterations_reached']):
                report.append('  Verified: no remaining qualifying gaps, no overlaps, no invalid geometry.')
            else:
                report.append('  NOT fully clean — see the counts above; no "no gaps and overlaps" claim is made.')

        # --------------------------------------------------------------
        # Output stage — layers 2 and 4 (layers 1 and 3 were created above)
        # --------------------------------------------------------------
        self._set_stage(96,99)
        out=None
        created=[]; not_created=[]
        if overlap_layer:
            created.append(f'Overlaps: {overlap_layer.name()} ({overlap_layer.featureCount()} feature(s))')
            out=overlap_layer
        elif self.dialog.fix_overlaps_checked():
            not_created.append('Overlaps layer: no overlaps found')
        if gap_result is not None and gap_result['gaps']:
            gap_layer=self._make_gap_layer(layer,gap_result['gaps'],self.dialog.gap_layer_name())
            created.append(f'Gaps: {gap_layer.name()} ({gap_layer.featureCount()} gap(s); field "corrected" shows which were eliminated)')
        else:
            self._remove_layers_named(self.dialog.gap_layer_name())
            if self.dialog.find_gaps_checked(): not_created.append('Gaps layer: no gaps found' if gap_tol>0 else 'Gaps layer: gap tolerance is 0')
        if no_overlap_layer is not None:
            created.append(f'Polygons without overlap: {no_overlap_layer.name()} ({no_overlap_layer.featureCount()} feature(s))')
            out=no_overlap_layer
        else:
            not_created.append('Polygons-without-overlap layer: nothing to repair')
        if gap_result is not None and gap_result['corrected_count']>0:
            final_layer=self._finalize_layer(self._build_output_layer(
                layer,geometries,self.dialog.final_layer_name(),original_geometries,added_by_fid))
            created.append(f'Polygons without gap and overlap: {final_layer.name()} ({final_layer.featureCount()} feature(s))')
            out=final_layer
        else:
            self._remove_layers_named(self.dialog.final_layer_name())
            if self.dialog.find_gaps_checked(): not_created.append('Polygons-without-gap-and-overlap layer: no gap was corrected')

        if self.dialog.zoom_checked() and out is not None and out.featureCount():
            self.iface.mapCanvas().setExtent(out.extent()); self.iface.mapCanvas().refresh()
        report += ['','Created layers:']+(['  '+c for c in created] if created else ['  none — nothing needed to be output'])
        if not_created:
            report += ['Not created:']+['  '+n for n in not_created]
        report.append('Operation completed successfully — 100%')
        self.dialog.set_report('\n'.join(report)); self.dialog.set_progress(100)

    # ------------------------------------------------------------------
    # Tab 2: Inward Buffer
    # ------------------------------------------------------------------
    def run_inward_buffers(self):
        layer=self.dialog.selected_layer()
        if not self._validate_layer_selected(layer): return
        if QgsWkbTypes.geometryType(layer.wkbType())!=QgsWkbTypes.PolygonGeometry:
            QMessageBox.warning(self.dialog,'Polygon layer required','Sequential inward buffers require a polygon layer.'); return
        feature_ids=self.dialog.processing_feature_ids(layer)
        if feature_ids is not None and not feature_ids:
            QMessageBox.warning(self.dialog,'No selected features','Select at least one feature in the input layer, or untick “Selected features only”.'); return
        settings=self.dialog.buffer_settings()
        if not settings:
            QMessageBox.warning(self.dialog,'No sections selected','Enable at least one buffer section.'); return
        try:
            distances=[geometry_tools.convert_distance(v,u,layer.crs()) for v,u in settings]
        except ValueError as e:
            QMessageBox.warning(self.dialog,'Distance / CRS problem',str(e)); return
        try:
            outputs=geometry_tools.sequential_inward_buffers(layer,distances,self.dialog.buffer_output_names(),self.dialog.buffer_remainder_name_value(),self.dialog.buffer_combined_name_value(),feature_ids=feature_ids)
            outputs=[self._finalize_layer(o) for o in outputs]
        except Exception as e:
            QMessageBox.critical(self.dialog,'Buffer failed',str(e)); return
        report=[f'Sequential inward buffers created from: {layer.name()}',f'Sections created: {len(settings)}','']
        report += [f'Section {i}: {v:g} {u} → {outputs[i-1].name()}' for i,(v,u) in enumerate(settings,1)]
        report += [f'Final remainder → {outputs[-2].name()}',f'Combined layer → {outputs[-1].name()}','',
                   'The Combined layer contains all sections and remainder as separate features; each feature can still be selected/highlighted independently.']
        if self.dialog.buffer_zoom_checked() and outputs[-1].featureCount():
            self.iface.mapCanvas().setExtent(outputs[-1].extent()); self.iface.mapCanvas().refresh()
        self.dialog.set_report('\n'.join(report)); self.dialog.set_progress(100)

    # ------------------------------------------------------------------
    # Tab 3: Line Layer Buffer
    # ------------------------------------------------------------------
    def run_line_buffers(self):
        layer=self.dialog.selected_layer()
        if not self._validate_layer_selected(layer): return
        if QgsWkbTypes.geometryType(layer.wkbType())!=QgsWkbTypes.LineGeometry:
            QMessageBox.warning(self.dialog,'Line layer required','Line Layer Buffer requires a line (or polyline) layer.'); return
        feature_ids=self.dialog.processing_feature_ids(layer)
        if feature_ids is not None and not feature_ids:
            QMessageBox.warning(self.dialog,'No selected features','Select at least one feature in the input layer, or untick “Selected features only”.'); return

        left_settings=self.dialog.line_left_settings(); right_settings=self.dialog.line_right_settings()
        if not left_settings and not right_settings:
            QMessageBox.warning(self.dialog,'No sections selected','Enable at least one section on the left side, the right side, or both.'); return

        all_outputs=[]; report=[f'Line layer buffered: {layer.name()}','']
        try:
            if left_settings:
                distances=[geometry_tools.convert_distance(v,u,layer.crs()) for v,u in left_settings]
                names=self.dialog.line_left_enabled_names()
                left_outputs=geometry_tools.sequential_line_side_buffers(layer,distances,'Left',names,feature_ids=feature_ids)
                left_outputs=[self._finalize_layer(l) for l in left_outputs]
                all_outputs += left_outputs
                report.append(f'Left side: {len(left_settings)} section(s) created.')
                report += [f'  Section {i}: {v:g} {u} → {left_outputs[i-1].name()}' for i,(v,u) in enumerate(left_settings,1)]
            if right_settings:
                distances=[geometry_tools.convert_distance(v,u,layer.crs()) for v,u in right_settings]
                names=self.dialog.line_right_enabled_names()
                right_outputs=geometry_tools.sequential_line_side_buffers(layer,distances,'Right',names,feature_ids=feature_ids)
                right_outputs=[self._finalize_layer(l) for l in right_outputs]
                all_outputs += right_outputs
                report.append(f'Right side: {len(right_settings)} section(s) created.')
                report += [f'  Section {i}: {v:g} {u} → {right_outputs[i-1].name()}' for i,(v,u) in enumerate(right_settings,1)]
        except ValueError as e:
            QMessageBox.warning(self.dialog,'Distance / CRS problem',str(e)); return
        except Exception as e:
            QMessageBox.critical(self.dialog,'Line buffer failed',str(e)); return

        if all_outputs:
            combined=geometry_tools.combine_layers(all_outputs,self.dialog.line_combined_name_value(),layer.crs())
            combined=self._finalize_layer(combined)
            report += ['',f'Combined layer → {combined.name()}']
            if self.dialog.line_zoom_checked() and combined.featureCount():
                self.iface.mapCanvas().setExtent(combined.extent()); self.iface.mapCanvas().refresh()
        self.dialog.set_report('\n'.join(report)); self.dialog.set_progress(100)

    # ------------------------------------------------------------------
    # Tab 4: Advance Digitization
    # ------------------------------------------------------------------
    def run_advance_digitization(self):
        if not self.dialog.merge_enabled() and not self.dialog.split_enabled():
            QMessageBox.warning(self.dialog,'Nothing enabled','Enable at least one operation in this tab.'); return
        report=[]

        if self.dialog.merge_enabled():
            layers=self.dialog.merge_selected_layers()
            if not layers:
                QMessageBox.warning(self.dialog,'No layers selected','Check at least one layer to merge.'); return
            selected_only=self.dialog.selected_features_only_checked()
            if selected_only:
                scoped_layers=[]
                total_selected=0
                for l in layers:
                    ids={f.id() for f in l.selectedFeatures()}
                    if ids:
                        scoped_layers.append(geometry_tools.copy_features_layer(l,ids,name=f'_GeoMend_selected_{l.name()}'))
                        total_selected += len(ids)
                if not scoped_layers:
                    QMessageBox.warning(self.dialog,'No selected features','Selected-features-only is enabled, but none of the layers checked for merge has selected features.')
                    return
                layers=scoped_layers
            try:
                result=geometry_tools.merge_vector_layers(layers,self.dialog.merge_output_name_value())
                report.append(f'Merged {len(layers)} layer(s): '+', '.join(l.name() for l in layers))
                if self.dialog.dissolve_checked():
                    result=geometry_tools.dissolve_layer(result,self.dialog.merge_output_name_value(),self.dialog.dissolve_field_value())
                    report.append('Dissolved output into a single feature' + (f' by field "{self.dialog.dissolve_field_value()}"' if self.dialog.dissolve_field_value() else '.'))
                if self.dialog.clean_dots_checked():
                    result=geometry_tools.clean_vertices(result,self.dialog.merge_output_name_value(),self.dialog.clean_tolerance_value())
                    report.append('Removed unwanted dots (duplicate/redundant vertices).')
                result=self._finalize_layer(result)
                report.append(f'Created: {result.name()} ({result.featureCount()} feature(s))')
            except Exception as e:
                QMessageBox.critical(self.dialog,'Merge / Dissolve / Clean failed',str(e)); return

        if self.dialog.split_enabled():
            src=self.dialog.split_source_layer()
            if not self._validate_layer_selected(src): return
            if self.dialog.selected_features_only_checked():
                ids={f.id() for f in src.selectedFeatures()}
                if not ids:
                    QMessageBox.warning(self.dialog,'No selected features','Selected-features-only is enabled, but the split source layer has no selected features.')
                    return
                src=geometry_tools.copy_features_layer(src,ids,name=f'_GeoMend_selected_{src.name()}')
            try:
                split_out=geometry_tools.explode_noncontiguous_parts(src,self.dialog.split_output_name_value())
                split_out=self._finalize_layer(split_out)
                report.append('')
                report.append(f'Split non-contiguous shapes in "{src.name()}" into independent fields.')
                report.append(f'Created: {split_out.name()} ({split_out.featureCount()} feature(s), from {src.featureCount()} source feature(s))')
            except Exception as e:
                QMessageBox.critical(self.dialog,'Split operation failed',str(e)); return

        self.dialog.set_report('\n'.join(report)); self.dialog.set_progress(100)

    # ------------------------------------------------------------------
    # Shared output builders
    # ------------------------------------------------------------------
    def _build_output_layer(self,src,geoms,name,original_geometries=None,gap_added=None):
        names_to_replace=[name]
        if name == 'GeoMend remaining polygons without overlap':
            # Remove the legacy underscore-named result so an old output cannot
            # be mistaken for the current corrected layer.
            names_to_replace.append('GeoMend_Remaining_Polygons_Without_Overlap')
        for output_name in names_to_replace:
            for old in QgsProject.instance().mapLayersByName(output_name):
                QgsProject.instance().removeMapLayer(old.id())
        out=QgsVectorLayer(f'{QgsWkbTypes.displayString(src.wkbType())}?crs={src.crs().authid()}',name,'memory')
        fields=[src.fields().at(i) for i in range(src.fields().count())]
        fields += [QgsField('source_fid',QVariant.Int), QgsField('original_area',QVariant.Double),
                   QgsField('output_area',QVariant.Double), QgsField('area_removed',QVariant.Double),
                   QgsField('gap_area_added',QVariant.Double),
                   QgsField('operation',QVariant.String)]
        out.dataProvider().addAttributes(fields); out.updateFields(); feats=[]
        original_geometries = original_geometries or {}
        for f in src.getFeatures():
            g=geoms.get(f.id())
            if g is None or g.isEmpty():
                continue
            nf=QgsFeature(out.fields()); nf.setGeometry(g)
            try: oa=original_geometries.get(f.id(), f.geometry()).area()
            except Exception: oa=0.0
            try: na=g.area() if QgsWkbTypes.geometryType(g.wkbType())==QgsWkbTypes.PolygonGeometry else 0.0
            except Exception: na=0.0
            ga=(gap_added or {}).get(f.id(),0.0)
            removed=max(0.0, oa-(na-ga))
            ops=[]
            if removed>0: ops.append('overlap cut / geometry changed')
            if ga>0: ops.append('gap incorporated')
            if not ops: ops=['processed']
            nf.setAttributes(list(f.attributes())+[f.id(),oa,na,removed,ga,'; '.join(ops)])
            feats.append(nf)
        out.dataProvider().addFeatures(feats); out.updateExtents(); return out

    @staticmethod
    def _remove_layers_named(*names):
        """Remove stale layers from an earlier run so a missing output can
        never be mistaken for a current one."""
        for n in names:
            for old in QgsProject.instance().mapLayersByName(n):
                QgsProject.instance().removeMapLayer(old.id())

    def _make_gap_layer(self,layer,gaps,name):
        """Polygon layer with the actual gap geometries and their status.

        Fields: gap_id, area, width (widest point), mean_width, kind
        (enclosed/open), neighbor_fids, corrected, target_fid, reason.
        A corrected gap is shown where it WAS; it no longer exists in the
        final polygons layer."""
        for old in QgsProject.instance().mapLayersByName(name):
            QgsProject.instance().removeMapLayer(old.id())
        out=QgsVectorLayer(f'Polygon?crs={layer.crs().authid()}',name,'memory')
        p=out.dataProvider()
        p.addAttributes([QgsField('gap_id',QVariant.Int),QgsField('area',QVariant.Double),
                         QgsField('width',QVariant.Double),QgsField('mean_width',QVariant.Double),
                         QgsField('kind',QVariant.String),QgsField('neighbor_fids',QVariant.String),
                         QgsField('corrected',QVariant.Bool),QgsField('target_fid',QVariant.Int),
                         QgsField('reason',QVariant.String)])
        out.updateFields(); feats=[]
        for g in gaps:
            geom=g.get('geometry')
            if geom is None or geom.isEmpty(): continue
            def fin(x): return x if (x is not None and x==x and x!=float('inf')) else None
            nf=QgsFeature(out.fields()); nf.setGeometry(geom)
            nf.setAttributes([g.get('gap_id'),g.get('area'),fin(g.get('width')),fin(g.get('mean_width')),
                              g.get('kind'),','.join(str(x) for x in g.get('neighbor_fids',[])),
                              bool(g.get('corrected')),g.get('target_fid'),g.get('reason','')])
            feats.append(nf)
        p.addFeatures(feats); out.updateExtents()
        return self._finalize_layer(out)

    def _make_overlap_layer(self,layer,overlaps,name):
        """Create a dedicated polygon layer containing the shared areas.

        This is intentionally separate from the corrected output: the corrected
        layer is overlap-free, while this layer preserves the original overlap
        geometry for inspection, measurement and editing.

        The raw pairwise intersections from find_overlaps() can themselves
        overlap one another wherever three or more source features share the
        same territory (A∩B and A∩C both cover the A∩B∩C zone). Those pairwise
        pieces are first dissolved into independent, non-overlapping regions
        via dissolve_overlaps_to_clean_regions() -- whether that yields a
        single merged shape or several separate ones -- so this output layer
        is guaranteed to contain no overlapping features of its own, exactly
        like the repaired main output.
        """
        old=QgsProject.instance().mapLayersByName(name)
        for x in old: QgsProject.instance().removeMapLayer(x.id())
        out=QgsVectorLayer(f'Polygon?crs={layer.crs().authid()}',name,'memory')
        p=out.dataProvider()
        p.addAttributes([
            QgsField('overlap_id',QVariant.Int),
            QgsField('source_fids',QVariant.String),
            QgsField('overlap_count',QVariant.Int),
            QgsField('overlap_area',QVariant.Double)
        ])
        out.updateFields()
        regions=geometry_tools.dissolve_overlaps_to_clean_regions(overlaps)
        feats=[]
        for i,region in enumerate(regions,1):
            geom=region.get('geometry')
            if not geom or geom.isEmpty():
                continue
            if QgsWkbTypes.geometryType(geom.wkbType())!=QgsWkbTypes.PolygonGeometry:
                geom=geometry_tools._extract_polygons(geom)
            if not geom or geom.isEmpty():
                continue
            nf=QgsFeature(out.fields())
            nf.setGeometry(geom)
            fids_str=','.join(str(f) for f in region.get('source_fids',[]))
            nf.setAttributes([i,fids_str,region.get('overlap_count',0),geom.area()])
            feats.append(nf)
        p.addFeatures(feats); out.updateExtents()
        out=self._finalize_layer(out)
        return out

