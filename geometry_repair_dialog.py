from qgis.PyQt.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QFormLayout,QGridLayout,QLabel,
    QComboBox,QCheckBox,QPushButton,QLineEdit,QPlainTextEdit,QProgressBar,QGroupBox,QTabWidget,
    QDoubleSpinBox,QToolButton,QListWidget,QListWidgetItem,QScrollArea,QWidget,QFrame,QSizePolicy,
    QFileDialog)
from qgis.core import QgsMapLayerProxyModel,QgsWkbTypes,QgsProject
from qgis.gui import QgsMapLayerComboBox
from qgis.PyQt.QtCore import Qt

PLUGIN_TITLE='GeoMend Toolkit'
BUILDER_CREDIT='Builder: Mohamed Taily Bah   |   Email: mtailybah@gmail.com'

# Prefix used for every default output-layer name the plugin suggests.
NAME_PREFIX='GeoMend'


class GeometryRepairDialog(QDialog):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.setWindowTitle(PLUGIN_TITLE)
        self.setMinimumSize(900,740)
        # Tracks, per tab (by tab name), whether that tab's Run has
        # completed at least once — so "Run Again" only ever appears on the
        # tab that was actually run, not on every tab in the dialog.
        self._tab_run_done={}
        self._tab_reports={}   # per-tab Output Report text (restored when a run tab is revisited)
        self._apply_style()
        self._build_ui()

    # ------------------------------------------------------------------
    # Expert-level visual styling
    # ------------------------------------------------------------------
    def _apply_style(self):
        self.setStyleSheet('''
            QDialog { background-color:#f4f6f8; }
            QGroupBox {
                font-weight:600;
                border:1px solid #c7ccd1;
                border-radius:6px;
                margin-top:10px;
                padding-top:8px;
                background-color:#ffffff;
            }
            QGroupBox::title {
                subcontrol-origin:margin;
                left:10px;
                padding:0 4px;
                color:#20344a;
            }
            QTabWidget::pane { border:1px solid #c7ccd1; border-radius:6px; background:#ffffff; }
            QTabBar::tab {
                padding:7px 16px;
                margin-right:2px;
                min-width:120px;
                border-top-left-radius:5px;
                border-top-right-radius:5px;
                background:#e3e7eb;
                color:#2c2c2c;
            }
            QTabBar::tab:selected { background:#ffffff; color:#20344a; border-bottom:2px solid #2d6a4f; }
            QPushButton, QToolButton {
                padding:5px 12px;
                border:1px solid #9aa4ae;
                border-radius:4px;
                background:#eef1f4;
            }
            QPushButton:hover, QToolButton:hover { background:#dde3e8; }
            QPushButton#runButton {
                background:#2d6a4f; color:white; font-weight:600; border:none;
            }
            QPushButton#runButton:hover { background:#245a42; }
        ''')

    # ------------------------------------------------------------------
    # Overall layout
    # ------------------------------------------------------------------
    def _build_ui(self):
        root=QVBoxLayout(self)

        header=QLabel(f'<b>{PLUGIN_TITLE}</b>')
        header.setStyleSheet('font-size:14px;')
        root.addWidget(header)

        common=QGroupBox('Input layer  (used by Inspect and Repair, Inward Buffer, Line Layer Buffer)')
        cf=QFormLayout(common)
        self.layer_search=QLineEdit(); self.layer_search.setPlaceholderText('Search layers...')
        cf.addRow('Search:',self.layer_search)
        self.layer_combo=QgsMapLayerComboBox(); self.layer_combo.setFilters(QgsMapLayerProxyModel.VectorLayer)
        cf.addRow('Layer:',self.layer_combo)
        self.layer_results=QListWidget(); self.layer_results.setMaximumHeight(80)
        cf.addRow('Search results:',self.layer_results)
        root.addWidget(common)

        # Shared feature-scope control.  This is intentionally placed directly
        # underneath the layer selector so the same choice is available to
        # Tabs 1–4 without duplicating a control in every tab.
        scope=QGroupBox('Feature scope (Tabs 1–4)')
        sf=QHBoxLayout(scope)
        self.chk_selected_only=QCheckBox('Selected features only')
        self.chk_selected_only.setToolTip(
            'When checked, operations in Tabs 1–4 process only the currently '
            'selected features of the relevant layer(s). Unselected features '
            'are left out of the operation/output.'
        )
        sf.addWidget(self.chk_selected_only)
        sf.addStretch(1)
        root.addWidget(scope)

        # Shared output-destination control, collapsed into a single row.
        # Leaving the folder empty keeps every layer the plugin creates as a
        # temporary/scratch layer (gone if not saved manually and QGIS/the
        # project closes). Choosing a folder makes every output a permanent
        # layer instead, written straight to disk there in the file type
        # picked, then reloaded so it behaves like any normal saved layer.
        dest=QGroupBox('Output Destination')
        df=QHBoxLayout(dest)
        df.addWidget(QLabel('Folder:'))
        self.output_folder_edit=QLineEdit(); self.output_folder_edit.setReadOnly(True)
        self.output_folder_edit.setPlaceholderText('Leave empty to keep new layers temporary')
        df.addWidget(self.output_folder_edit,1)
        self.output_folder_btn=QToolButton(); self.output_folder_btn.setText('Browse…')
        df.addWidget(self.output_folder_btn)
        self.output_folder_clear_btn=QToolButton(); self.output_folder_clear_btn.setText('Clear')
        df.addWidget(self.output_folder_clear_btn)
        df.addWidget(QLabel('File type:'))
        self.output_filetype_combo=QComboBox()
        self.output_filetype_combo.addItems(['GeoPackage (.gpkg)','Esri Shapefile (.shp)'])
        df.addWidget(self.output_filetype_combo)
        dest.setToolTip(
            'Leave the folder empty: every layer this plugin creates, in any '
            'tab, stays a temporary/scratch layer for this QGIS session only.\n'
            'Choose a folder: every layer becomes permanent instead, written '
            'to that folder in the file type selected, then reloaded so it '
            'is already a normal saved layer.'
        )
        root.addWidget(dest)

        self.output_folder_btn.clicked.connect(self._choose_output_folder)
        self.output_folder_clear_btn.clicked.connect(self._clear_output_folder)
        self.output_folder_edit.textChanged.connect(self._sync_output_destination)
        self._sync_output_destination()

        self.layer_search.textChanged.connect(self._filter_layers)
        self.layer_results.itemClicked.connect(self._choose_search_layer)

        self.tabs=QTabWidget(); root.addWidget(self.tabs,stretch=1)
        self.tabs.setElideMode(Qt.ElideNone)
        self.tabs.tabBar().setUsesScrollButtons(True)
        self.tabs.tabBar().setExpanding(False)
        self._build_tab_inspect_repair()
        self._build_tab_inward_buffer()
        self._build_tab_line_buffer()
        self._build_tab_advance_digitization()
        self._build_tab_help()

        report=QGroupBox('Output Report')
        rf=QVBoxLayout(report)
        self.report_box=QPlainTextEdit(); self.report_box.setReadOnly(True)
        self.report_box.setFixedHeight(140)
        rf.addWidget(self.report_box)
        root.addWidget(report)

        self.progress=QProgressBar(); root.addWidget(self.progress)

        footer=QHBoxLayout()
        credit=QLabel(BUILDER_CREDIT); credit.setStyleSheet('color:gray;font-size:10px;')
        footer.addWidget(credit)
        footer.addStretch(1)
        self.run_btn=QPushButton('Run'); self.run_btn.setMinimumWidth(100); self.run_btn.setObjectName('runButton')
        self.close_btn=QPushButton('Close'); self.close_btn.setMinimumWidth(100)
        self.close_btn.clicked.connect(self.reject)
        footer.addWidget(self.run_btn)
        footer.addWidget(self.close_btn)
        root.addLayout(footer)

        self.tabs.currentChanged.connect(self._sync_run_button)
        self._sync_run_button(self.tabs.currentIndex())

    @staticmethod
    def _wrap_scroll(widget):
        scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(widget)
        return scroll

    def _sync_run_button(self,index):
        # Help tab has nothing to run, so the Run button is hidden there.
        name=self.tabs.tabText(index)
        is_help=name=='Help'
        self.run_btn.setVisible(not is_help)
        self.run_btn.setEnabled(not is_help)
        if not is_help:
            self.run_btn.setText('Run Again' if self._tab_run_done.get(name,False) else 'Run')
        self._sync_progress_display(index)

    def _sync_progress_display(self,index):
        """Keep the progress bar in step with whichever tab is now active:
        0% if that tab hasn't been run yet (or is Help), 100% if it has
        already completed a run — restarting/matching per tab rather than
        carrying over the value from whatever tab was open before."""
        name=self.tabs.tabText(index)
        done=self._tab_run_done.get(name,False)
        self.set_progress(100 if done else 0)
        # Output Report restarts on a tab that has not been run, and reappears
        # on a tab that already has been.
        self.report_box.setPlainText(self._tab_reports.get(name,'') if done else '')

    def current_tab_name(self):
        return self.tabs.tabText(self.tabs.currentIndex())

    # ------------------------------------------------------------------
    # Output destination (temporary layer vs save to disk)
    # ------------------------------------------------------------------
    def _sync_output_destination(self,*_):
        to_disk=bool(self.output_folder_edit.text().strip())
        self.output_filetype_combo.setEnabled(to_disk)
        self.output_folder_clear_btn.setEnabled(to_disk)

    def _choose_output_folder(self):
        folder=QFileDialog.getExistingDirectory(self,'Choose output folder',self.output_folder_edit.text() or '')
        if folder:
            self.output_folder_edit.setText(folder)

    def _clear_output_folder(self):
        self.output_folder_edit.setText('')

    # ------------------------------------------------------------------
    # Tab 1: Inspect and Repair
    # ------------------------------------------------------------------
    def _build_tab_inspect_repair(self):
        w=QWidget(); root=QVBoxLayout(w)

        # Compact, linear layout: one row per check = tick box + its layer name
        # (and tolerance for gaps). No explanatory text.
        grid=QGridLayout(); grid.setColumnStretch(1,1)
        row=0
        self.chk_invalid=QCheckBox('Invalid geometry'); self.chk_invalid.setChecked(True)
        grid.addWidget(self.chk_invalid,row,0); row+=1

        self.chk_overlaps=QCheckBox('Overlap'); self.chk_overlaps.setChecked(True)
        self.overlap_layer_name_edit=QLineEdit('GeoMend_Overlap_Areas')
        grid.addWidget(self.chk_overlaps,row,0); grid.addWidget(self.overlap_layer_name_edit,row,1); row+=1

        self.chk_gaps=QCheckBox('Gap'); self.chk_gaps.setChecked(True)
        self.gap_layer_name_edit=QLineEdit('GeoMend gaps')
        self.gap_tolerance=QDoubleSpinBox(); self.gap_tolerance.setRange(0.0,100000); self.gap_tolerance.setDecimals(4)
        self.gap_tolerance.setSingleStep(0.1); self.gap_tolerance.setValue(0.0); self.gap_tolerance.setToolTip('Gap tolerance (maximum gap width). 0 = gap check skipped.')
        self.gap_tolerance_units=QComboBox(); self.gap_tolerance_units.addItems(['Meters','Feet'])
        grid.addWidget(self.chk_gaps,row,0); grid.addWidget(self.gap_layer_name_edit,row,1)
        grid.addWidget(self.gap_tolerance,row,2); grid.addWidget(self.gap_tolerance_units,row,3); row+=1

        self.chk_gap_sliver=QCheckBox('Tapering slivers'); self.chk_gap_sliver.setChecked(True)
        self.chk_gap_sliver.setToolTip('Also close long tapering slivers (average width ≤ tolerance, widest point ≤ 2× tolerance).')
        grid.addWidget(self.chk_gap_sliver,row,0); row+=1

        self.chk_duplicates=QCheckBox('Duplicate'); self.chk_duplicates.setChecked(True)
        grid.addWidget(self.chk_duplicates,row,0); row+=1

        self.output_layer_name_edit=QLineEdit('GeoMend remaining polygons without overlap')
        grid.addWidget(QLabel('Without overlap'),row,0); grid.addWidget(self.output_layer_name_edit,row,1); row+=1
        self.final_layer_name_edit=QLineEdit('GeoMend polygons without gap and overlap')
        grid.addWidget(QLabel('Without gap and overlap'),row,0); grid.addWidget(self.final_layer_name_edit,row,1); row+=1

        self.chk_report_only=QCheckBox('Report only')
        grid.addWidget(self.chk_report_only,row,0); row+=1
        root.addLayout(grid)

        self.adv_toggle=QToolButton(); self.adv_toggle.setText('Advanced ▾'); self.adv_toggle.setCheckable(True)
        self.adv_toggle.setToolButtonStyle(Qt.ToolButtonTextOnly)
        root.addWidget(self.adv_toggle)
        adv=QGroupBox(); af=QFormLayout(adv)
        self.chk_cross_layer_overlap=QCheckBox('Cut overlap against layers below')
        af.addRow(self.chk_cross_layer_overlap)
        self.chk_snap=QCheckBox('Snap to layers underneath')
        af.addRow(self.chk_snap)
        self.snap_distance=QDoubleSpinBox(); self.snap_distance.setRange(0.000001,100000000); self.snap_distance.setDecimals(6); self.snap_distance.setValue(0.10)
        self.snap_units=QComboBox(); self.snap_units.addItems(['Meters','Feet'])
        snap_row=QHBoxLayout(); snap_row.addWidget(self.snap_distance); snap_row.addWidget(self.snap_units)
        af.addRow('Snap tolerance:',snap_row)
        root.addWidget(adv); adv.setVisible(False); self.adv_toggle.toggled.connect(adv.setVisible)

        self.chk_zoom=QCheckBox('Zoom to result'); self.chk_zoom.setChecked(True)
        root.addWidget(self.chk_zoom)

        root.addStretch(1)
        self.tabs.addTab(self._wrap_scroll(w),'Inspect and Repair')

    # ------------------------------------------------------------------
    # Tab 2: Inward Buffer  (two columns: enable/sequential sections | output names)
    # ------------------------------------------------------------------
    def _build_tab_inward_buffer(self):
        w=QWidget(); root=QVBoxLayout(w)
        note=QLabel('Create the enabled number of sequential inward buffer sections. Section 2 starts exactly where Section 1 ends, and so on.')
        note.setWordWrap(True); root.addWidget(note)

        split=QHBoxLayout(); root.addLayout(split)

        left=QGroupBox('Sequential inward buffer sections'); ll=QVBoxLayout(left)
        right=QGroupBox('Output layer name for each enabled section'); rl=QVBoxLayout(right)
        split.addWidget(left,stretch=1); split.addWidget(right,stretch=1)

        self.buffer_enable=[]; self.buffer_distance=[]; self.buffer_units=[]; self.buffer_names=[]
        for i in range(5):
            lrow=QHBoxLayout()
            chk=QCheckBox(f'Section {i+1}'); chk.setChecked(i==0)
            dist=QDoubleSpinBox(); dist.setRange(0.000001,100000000); dist.setDecimals(6); dist.setValue(1.0)
            units=QComboBox(); units.addItems(['Meters','Feet'])
            lrow.addWidget(chk); lrow.addWidget(QLabel('Distance:')); lrow.addWidget(dist); lrow.addWidget(units)
            ll.addLayout(lrow)
            self.buffer_enable.append(chk); self.buffer_distance.append(dist); self.buffer_units.append(units)

            rrow=QHBoxLayout()
            rrow.addWidget(QLabel(f'Section {i+1} name:'))
            e=QLineEdit(f'inward_buffer_{i+1}'); rrow.addWidget(e)
            rl.addLayout(rrow)
            self.buffer_names.append(e)

        ll.addStretch(1)
        rem_row=QHBoxLayout(); rem_row.addWidget(QLabel('Remainder name:'))
        self.buffer_remainder_name=QLineEdit('inward_buffer_remainder'); rem_row.addWidget(self.buffer_remainder_name)
        rl.addLayout(rem_row)
        comb_row=QHBoxLayout(); comb_row.addWidget(QLabel('Combined name:'))
        self.buffer_combined_name=QLineEdit('inward_buffer_combined'); comb_row.addWidget(self.buffer_combined_name)
        rl.addLayout(comb_row)
        rl.addStretch(1)

        self.chk_buffer_zoom=QCheckBox('Zoom to result'); self.chk_buffer_zoom.setChecked(True)
        root.addWidget(self.chk_buffer_zoom)
        root.addStretch(1)
        self.tabs.addTab(self._wrap_scroll(w),'Inward Buffer')

    # ------------------------------------------------------------------
    # Tab 3: Line Layer Buffer  (left side | right side)
    # ------------------------------------------------------------------
    def _build_tab_line_buffer(self):
        w=QWidget(); root=QVBoxLayout(w)
        note=QLabel('Buffer a line layer on the left side, the right side, or both. Enable one side only for a single-sided buffer, or both for a two-sided buffer. Each section continues exactly where the previous one on that side ends — the same principle as the Inward Buffer.')
        note.setWordWrap(True); root.addWidget(note)

        split=QHBoxLayout(); root.addLayout(split)
        self.line_left_enable=[]; self.line_left_distance=[]; self.line_left_units=[]; self.line_left_name_edits=[]
        self.line_right_enable=[]; self.line_right_distance=[]; self.line_right_units=[]; self.line_right_name_edits=[]

        left=self._build_line_side_box('Left side buffer',self.line_left_enable,self.line_left_distance,self.line_left_units,self.line_left_name_edits,'line_buffer_L')
        right=self._build_line_side_box('Right side buffer',self.line_right_enable,self.line_right_distance,self.line_right_units,self.line_right_name_edits,'line_buffer_R')
        split.addWidget(left,stretch=1); split.addWidget(right,stretch=1)

        comb_row=QHBoxLayout(); comb_row.addWidget(QLabel('Combined output layer name:'))
        self.line_combined_name=QLineEdit('line_buffer_combined'); comb_row.addWidget(self.line_combined_name)
        root.addLayout(comb_row)

        self.chk_line_zoom=QCheckBox('Zoom to result'); self.chk_line_zoom.setChecked(True)
        root.addWidget(self.chk_line_zoom)
        root.addStretch(1)
        self.tabs.addTab(self._wrap_scroll(w),'Line Layer Buffer')

    def _build_line_side_box(self,title,enable_list,distance_list,units_list,names_list,default_prefix):
        box=QGroupBox(title); bl=QVBoxLayout(box)
        for i in range(3):
            row=QHBoxLayout()
            chk=QCheckBox(f'Section {i+1}'); chk.setChecked(i==0 and title.startswith('Left'))
            dist=QDoubleSpinBox(); dist.setRange(0.000001,100000000); dist.setDecimals(6); dist.setValue(1.0)
            units=QComboBox(); units.addItems(['Meters','Feet'])
            row.addWidget(chk); row.addWidget(QLabel('Distance:')); row.addWidget(dist); row.addWidget(units)
            bl.addLayout(row)
            name_row=QHBoxLayout(); name_row.addWidget(QLabel('Name:'))
            name_edit=QLineEdit(f'{default_prefix}{i+1}'); name_row.addWidget(name_edit)
            bl.addLayout(name_row)
            enable_list.append(chk); distance_list.append(dist); units_list.append(units); names_list.append(name_edit)
        bl.addStretch(1)
        return box

    # ------------------------------------------------------------------
    # Tab 4: Advance Digitization
    # ------------------------------------------------------------------
    def _build_tab_advance_digitization(self):
        w=QWidget(); root=QVBoxLayout(w)

        merge_box=QGroupBox('Merge, Dissolve && Remove Unwanted Dots'); ml=QVBoxLayout(merge_box)
        self.chk_merge_enable=QCheckBox('Enable this operation'); self.chk_merge_enable.setChecked(True)
        ml.addWidget(self.chk_merge_enable)

        layer_row=QHBoxLayout()
        self.merge_layer_list=QListWidget(); self.merge_layer_list.setMaximumHeight(110)
        layer_row.addWidget(self.merge_layer_list,stretch=1)
        btn_col=QVBoxLayout()
        self.merge_refresh_btn=QToolButton(); self.merge_refresh_btn.setText('⟳')
        self.merge_refresh_btn.setFixedSize(32,32)
        self.merge_refresh_btn.setToolTip('Refresh layer list — reloads this list with every vector layer currently in the project.')
        self.merge_refresh_btn.clicked.connect(self.refresh_merge_layer_list)
        btn_col.addWidget(self.merge_refresh_btn); btn_col.addStretch(1)
        layer_row.addLayout(btn_col)
        ml.addWidget(QLabel('Vector layers to merge (check all that apply):'))
        ml.addLayout(layer_row)

        self.chk_dissolve=QCheckBox('Dissolve output into a single feature'); ml.addWidget(self.chk_dissolve)
        dissolve_row=QHBoxLayout(); dissolve_row.addWidget(QLabel('Dissolve by field (optional):'))
        self.dissolve_field=QLineEdit(); dissolve_row.addWidget(self.dissolve_field)
        ml.addLayout(dissolve_row)

        self.chk_clean_dots=QCheckBox('Remove unwanted dots (duplicate/redundant vertices)'); self.chk_clean_dots.setChecked(True)
        ml.addWidget(self.chk_clean_dots)
        tol_row=QHBoxLayout(); tol_row.addWidget(QLabel('Vertex tolerance:'))
        self.clean_tolerance=QDoubleSpinBox(); self.clean_tolerance.setRange(0.0,1000.0); self.clean_tolerance.setDecimals(6); self.clean_tolerance.setValue(0.0)
        tol_row.addWidget(self.clean_tolerance); ml.addLayout(tol_row)

        merge_out_row=QHBoxLayout(); merge_out_row.addWidget(QLabel('Output layer name:'))
        self.merge_output_name=QLineEdit('GeoMend_Merged_Clean'); merge_out_row.addWidget(self.merge_output_name)
        ml.addLayout(merge_out_row)
        root.addWidget(merge_box)

        split_box=QGroupBox('Split Non-Contiguous Shapes Into Independent Fields'); sl=QVBoxLayout(split_box)
        self.chk_split_enable=QCheckBox('Enable this operation'); self.chk_split_enable.setChecked(True)
        sl.addWidget(self.chk_split_enable)
        sl.addWidget(QLabel('If a single field is actually made up of separate, non-touching shapes, each shape is cut out into its own independent field. Shapes that touch or overlap are kept together as one whole.'))
        split_layer_row=QFormLayout()
        self.split_layer_combo=QgsMapLayerComboBox(); self.split_layer_combo.setFilters(QgsMapLayerProxyModel.VectorLayer)
        split_layer_row.addRow('Source layer:',self.split_layer_combo)
        self.split_output_name=QLineEdit('GeoMend_Split_Independent_Fields')
        split_layer_row.addRow('Output layer name:',self.split_output_name)
        sl.addLayout(split_layer_row)
        root.addWidget(split_box)

        root.addStretch(1)
        self.tabs.addTab(self._wrap_scroll(w),'Advance Digitization')

    def refresh_merge_layer_list(self):
        self.merge_layer_list.clear()
        for layer in QgsProject.instance().mapLayers().values():
            if hasattr(layer,'geometryType'):
                item=QListWidgetItem(layer.name())
                item.setFlags(item.flags()|Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Unchecked)
                item.setData(Qt.UserRole,layer.id())
                self.merge_layer_list.addItem(item)

    # ------------------------------------------------------------------
    # Tab 5: Help
    # ------------------------------------------------------------------
    def _build_tab_help(self):
        w=QWidget(); root=QVBoxLayout(w)
        scroll=QScrollArea(); scroll.setWidgetResizable(True)
        content=QLabel()
        content.setWordWrap(True)
        content.setTextFormat(Qt.RichText)
        content.setText(f'''
<h3>{PLUGIN_TITLE} — Quick Guide</h3>
<p align="justify">Pick a layer in the <b>Input layer</b> box at the top (used by the first three tabs), open a tab, set the options, then press <b>Run</b> in the bottom-right corner. <b>Close</b> exits the plugin.</p>

<p align="justify"><b>Output Destination</b><br>
One control decides how every layer this plugin creates — in any tab — is handed to you.
Leave the <b>Folder</b> field empty and every output stays a <b>temporary</b> scratch layer
in this QGIS session only (fastest, but lost if you close QGIS or the project without
saving it yourself). Pick a folder with <b>Browse…</b> and every output instead becomes a
<b>permanent</b> layer, written straight to that folder in the format chosen under
<b>File type</b> (GeoPackage or Esri Shapefile), then reopened so it is already a normal
saved layer with nothing further to do. <b>Clear</b> empties the folder again to go back to
temporary layers. Re-running with the same output names/folder replaces the previous
file(s) of the same name, just like re-running replaces a previous temporary layer.</p>

<p align="justify"><b>Inspect and Repair</b><br>
Checks the selected layer for invalid geometry, overlaps, gaps and duplicate features.
Invalid geometry is automatically repaired and duplicate features are removed from the
final output before it is written.<br>
<i>Overlap Section</i> — when Overlap is enabled, exactly two layers are produced: an
<b>Overlap Areas</b> layer containing the exact overlapping polygon areas (every separate
overlap location, including places where three or more features share the same territory),
and a <b>Remaining Polygons Without Overlap</b> layer — the refined version of the input
polygons with every overlapping portion subtracted, guaranteed to contain zero overlap
between any of its features. No separate "Problem Areas" layer is created.<br>
Inspect and Repair produces up to <b>four layers</b> — a layer is only created when its check actually found something; otherwise the result is stated in the Output Report only: <b>1. Overlap Areas</b>, <b>2. Gaps</b> (every gap found, with gap_id, area, width, neighbor_fids, corrected, target_fid and a reason when it was not corrected), <b>3. Remaining Polygons Without Overlap</b> and <b>4. Polygons Without Gap and Overlap</b>.<br>
Gap correction physically incorporates qualifying gaps no wider than the Gap tolerance (default 0 = gap check skipped; enter a value to enable it; resets to 0 whenever the plugin is closed) into the appropriate neighboring polygon. The actual gap area is added to the final polygon geometry; larger gaps and legitimate holes of a single polygon are not modified. No separate gap layer is created; the final result is checked for remaining gaps, overlaps and invalid geometry. The final corrected geometry is written to <b>GeoMend remaining polygons without overlap</b>.</p>

<p align="justify"><b>Inward Buffer</b><br>
Enable the sequential inward buffer sections you need on the left, and give each enabled
section its own output layer name on the right. Every section starts exactly where the
previous one finished, working inward from the outer edge of the polygon.</p>

<p align="justify"><b>Line Layer Buffer</b><br>
Buffer a line layer on the left side, the right side, or both — just enable sections on
either side of the panel. Distances can be entered in meters or feet. Sections on the same
side continue on from one another exactly like the Inward Buffer.</p>

<p align="justify"><b>Advance Digitization</b><br>
<i>Merge, Dissolve &amp; Remove Unwanted Dots</i> — combine several vector layers into one,
optionally dissolve them into a single shape (or dissolve by an attribute), and clean up
stray/duplicate vertices left over from digitizing.<br>
<i>Split Non-Contiguous Shapes</i> — if what looks like one field is really several separate,
disconnected shapes grouped as a single feature, this splits each disconnected shape into
its own independent field. Shapes that touch or overlap stay together as one whole.</p>

<p align="justify"><b>Run / Close</b><br>
Run performs the action for whichever tab is currently open. After the first successful run,
the button changes to <b>Run Again</b>. Close exits the dialog.</p>

<hr>
<p style="color:gray;font-size:11px;">{BUILDER_CREDIT}</p>
''')
        scroll.setWidget(content)
        root.addWidget(scroll)
        self.tabs.addTab(w,'Help')

    # ------------------------------------------------------------------
    # Layer search helpers
    # ------------------------------------------------------------------
    def refresh_layer_search(self):
        self._filter_layers(self.layer_search.text())
        self.refresh_merge_layer_list()

    def select_active_layer(self,layer):
        """Set the Layer selector to `layer` (the layer active/selected in
        the QGIS Layers panel) and clear any leftover search text/results so
        the selector plainly shows it."""
        self.layer_search.setText('')
        self.layer_combo.setLayer(layer)

    def _filter_layers(self,text):
        self.layer_results.clear()
        needle=text.strip().lower()
        for i in range(self.layer_combo.count()):
            name=self.layer_combo.itemText(i)
            if not needle or needle in name.lower():
                self.layer_results.addItem(name)

    def _choose_search_layer(self,item):
        target=item.text()
        for i in range(self.layer_combo.count()):
            if self.layer_combo.itemText(i)==target:
                self.layer_combo.setCurrentIndex(i); break

    # ------------------------------------------------------------------
    # Accessors used by the plugin controller
    # ------------------------------------------------------------------
    def selected_layer(self): return self.layer_combo.currentLayer()
    def selected_features_only_checked(self): return self.chk_selected_only.isChecked()
    def processing_feature_ids(self,layer):
        if not self.chk_selected_only.isChecked():
            return None
        return {f.id() for f in layer.selectedFeatures()}

    # Inspect and Repair
    def fix_invalid_checked(self): return self.chk_invalid.isChecked()
    def fix_overlaps_checked(self): return self.chk_overlaps.isChecked()
    def find_gaps_checked(self): return self.chk_gaps.isChecked()
    def gap_sliver_checked(self): return self.chk_gap_sliver.isChecked()
    def gap_layer_name(self): return self.gap_layer_name_edit.text().strip() or 'GeoMend gaps'
    def final_layer_name(self): return self.final_layer_name_edit.text().strip() or 'GeoMend polygons without gap and overlap'
    def reset_run_state(self):
        """Put the Run button back to a fresh 'Run' state (called whenever the
        dialog is opened or closed)."""
        self._tab_run_done={}; self._tab_reports={}
        self.gap_tolerance.setValue(0.0); self.gap_tolerance_units.setCurrentIndex(0)
        self.run_btn.setEnabled(self.current_tab_name()!='Help')
        self._sync_run_button(self.tabs.currentIndex())
        self.set_progress(0)
    def done(self,r):
        self.reset_run_state(); super().done(r)
    def closeEvent(self,e):
        self.reset_run_state(); super().closeEvent(e)
    def gap_tolerance_value(self): return self.gap_tolerance.value()
    def gap_tolerance_unit(self): return self.gap_tolerance_units.currentText()
    def remove_duplicates_checked(self): return self.chk_duplicates.isChecked()
    def report_only_checked(self): return self.chk_report_only.isChecked()
    def cross_layer_overlap_checked(self): return self.chk_cross_layer_overlap.isChecked()
    def snap_checked(self): return self.chk_snap.isChecked()
    def snap_distance_value(self): return self.snap_distance.value()
    def snap_unit_value(self): return self.snap_units.currentText()
    def zoom_checked(self): return self.chk_zoom.isChecked()
    def output_layer_name(self): return self.output_layer_name_edit.text().strip() or 'GeoMend remaining polygons without overlap'
    def overlap_layer_name(self): return self.overlap_layer_name_edit.text().strip() or 'GeoMend_Overlap_Areas'
    def mark_run_completed(self):
        self._tab_run_done[self.current_tab_name()]=True
        self._sync_run_button(self.tabs.currentIndex())

    # Inward Buffer
    def buffer_settings(self): return [(self.buffer_distance[i].value(),self.buffer_units[i].currentText()) for i in range(5) if self.buffer_enable[i].isChecked()]
    def buffer_output_names(self): return [e.text().strip() or f'inward_buffer_{i+1}' for i,e in enumerate(self.buffer_names)]
    def buffer_remainder_name_value(self): return self.buffer_remainder_name.text().strip() or 'inward_buffer_remainder'
    def buffer_combined_name_value(self): return self.buffer_combined_name.text().strip() or 'inward_buffer_combined'
    def buffer_zoom_checked(self): return self.chk_buffer_zoom.isChecked()

    # Line Layer Buffer
    def line_left_settings(self): return [(self.line_left_distance[i].value(),self.line_left_units[i].currentText()) for i in range(len(self.line_left_enable)) if self.line_left_enable[i].isChecked()]
    def line_left_names(self): return [e.text().strip() or f'line_buffer_L{i+1}' for i,e in enumerate(self.line_left_name_edits)]
    def line_right_settings(self): return [(self.line_right_distance[i].value(),self.line_right_units[i].currentText()) for i in range(len(self.line_right_enable)) if self.line_right_enable[i].isChecked()]
    def line_right_names(self): return [e.text().strip() or f'line_buffer_R{i+1}' for i,e in enumerate(self.line_right_name_edits)]
    def line_left_enabled_names(self): return [self.line_left_name_edits[i].text().strip() or f'line_buffer_L{i+1}' for i in range(len(self.line_left_enable)) if self.line_left_enable[i].isChecked()]
    def line_right_enabled_names(self): return [self.line_right_name_edits[i].text().strip() or f'line_buffer_R{i+1}' for i in range(len(self.line_right_enable)) if self.line_right_enable[i].isChecked()]
    def line_combined_name_value(self): return self.line_combined_name.text().strip() or 'line_buffer_combined'
    def line_zoom_checked(self): return self.chk_line_zoom.isChecked()

    # Advance Digitization
    def merge_enabled(self): return self.chk_merge_enable.isChecked()
    def merge_selected_layers(self):
        ids=[]
        for i in range(self.merge_layer_list.count()):
            item=self.merge_layer_list.item(i)
            if item.checkState()==Qt.Checked:
                ids.append(item.data(Qt.UserRole))
        layers=[QgsProject.instance().mapLayer(i) for i in ids]
        return [l for l in layers if l is not None]
    def dissolve_checked(self): return self.chk_dissolve.isChecked()
    def dissolve_field_value(self): return self.dissolve_field.text().strip() or None
    def clean_dots_checked(self): return self.chk_clean_dots.isChecked()
    def clean_tolerance_value(self): return self.clean_tolerance.value()
    def merge_output_name_value(self): return self.merge_output_name.text().strip() or 'GeoMend_Merged_Clean'
    def split_enabled(self): return self.chk_split_enable.isChecked()
    def split_source_layer(self): return self.split_layer_combo.currentLayer()
    def split_output_name_value(self): return self.split_output_name.text().strip() or 'GeoMend_Split_Independent_Fields'

    # Shared
    def set_progress(self,v): self.progress.setValue(int(v))
    def set_report(self,t):
        self._tab_reports[self.current_tab_name()]=t
        self.report_box.setPlainText(t)
    def output_to_disk(self): return bool(self.output_folder_edit.text().strip())
    def output_folder(self): return self.output_folder_edit.text().strip()
    def output_file_driver(self):
        return {'GeoPackage (.gpkg)':'GPKG','Esri Shapefile (.shp)':'ESRI Shapefile'}.get(
            self.output_filetype_combo.currentText(),'GPKG')
