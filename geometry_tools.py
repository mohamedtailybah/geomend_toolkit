import os
from qgis.core import (QgsGeometry,QgsSpatialIndex,QgsFeature,QgsWkbTypes,QgsUnitTypes,
    QgsVectorLayer,QgsProject,QgsField,QgsCoordinateTransform,QgsRectangle,QgsVectorFileWriter,QgsPointXY)
from qgis.PyQt.QtCore import QVariant

# Buffer side constants for single-sided line buffers (QGIS-version tolerant;
# these names were referenced but never defined in the original module).
try:
    from qgis.core import Qgis
    SIDE_LEFT=Qgis.BufferSide.Left; SIDE_RIGHT=Qgis.BufferSide.Right
except Exception:
    try:
        SIDE_LEFT=QgsGeometry.SideLeft; SIDE_RIGHT=QgsGeometry.SideRight
    except Exception:
        SIDE_LEFT=0; SIDE_RIGHT=1


def check_validity(layer,feedback=None):
    problems=[]; total=layer.featureCount() or 1
    for i,f in enumerate(layer.getFeatures()):
        g=f.geometry()
        if g is None or g.isEmpty():
            problems.append({'fid':f.id(),'reason':'Empty/null geometry','geometry':None}); continue
        for e in g.validateGeometry():
            problems.append({'fid':f.id(),'reason':e.what(),'geometry':_problem_geometry_from_error(e,g)})
        if feedback: feedback.setProgress(int(100*(i+1)/total))
    return problems

def _problem_geometry_from_error(err,g):
    try:
        p=err.where() if err.hasWhere() else None
        if p and hasattr(p,'x'): return QgsGeometry.fromPointXY(p)
    except Exception: pass
    try: return g.pointOnSurface()
    except Exception: return None

def fix_invalid_geometry(g):
    if g is None or g.isEmpty(): return g,False,True
    if not g.validateGeometry(): return g,False,False
    original=QgsGeometry(g); fixed=None
    try: fixed=g.makeValid()
    except Exception: pass
    if fixed is None or fixed.isEmpty():
        try: fixed=g.buffer(0,8)
        except Exception: fixed=None
    if fixed is None or fixed.isEmpty(): return original,False,True
    if QgsWkbTypes.geometryType(original.wkbType())==QgsWkbTypes.PolygonGeometry and QgsWkbTypes.geometryType(fixed.wkbType())!=QgsWkbTypes.PolygonGeometry:
        fixed=_extract_polygons(fixed)
        if fixed is None or fixed.isEmpty(): return original,False,True
    return fixed,True,bool(fixed.validateGeometry())

def _extract_polygons(g):
    parts=g.asGeometryCollection() if g.isMultipart() else [g]
    polys=[x for x in parts if QgsWkbTypes.geometryType(x.wkbType())==QgsWkbTypes.PolygonGeometry]
    return None if not polys else (polys[0] if len(polys)==1 else QgsGeometry.unaryUnion(polys))

def _explode_to_singlepart_polygons(g):
    """Split a (possibly multipart) polygon geometry into a list of
    independent single-part polygon QgsGeometry objects.

    Used to turn a unioned/dissolved geometry into individual disjoint
    features so that none of the output features overlap one another.
    """
    if g is None or g.isEmpty():
        return []
    parts=g.asGeometryCollection() if g.isMultipart() else [g]
    result=[]
    for part in parts:
        if QgsWkbTypes.geometryType(part.wkbType())!=QgsWkbTypes.PolygonGeometry:
            continue
        if part.isEmpty():
            continue
        result.append(part)
    return result

def dissolve_overlaps_to_clean_regions(overlaps):
    """Turn the raw pairwise overlap list from find_overlaps() into a set of
    independent, non-overlapping regions -- regardless of whether there is a
    single overlapping pair or many overlapping areas (including places where
    three or more source features overlap the same territory).

    Pairwise intersections can themselves overlap each other (e.g. A∩B and
    A∩C both contain the A∩B∩C zone). Unioning every pairwise overlap first
    removes that double coverage, then each disjoint part of the union is
    emitted as its own feature so the resulting layer is guaranteed overlap
    free, whether that means one merged shape or several separate ones.

    Returns a list of dicts: {'geometry','area','source_fids','overlap_count'}
    """
    geoms=[x.get('geometry') for x in overlaps if x.get('geometry') and not x['geometry'].isEmpty()]
    union=_safe_union(geoms)
    if union is None:
        return []
    parts=_explode_to_singlepart_polygons(union)
    regions=[]
    for part in parts:
        fids=set()
        contributing=0
        for x in overlaps:
            og=x.get('geometry')
            if og is None or og.isEmpty():
                continue
            try:
                touches=part.intersects(og) and part.intersection(og).area()>0
            except Exception:
                touches=False
            if touches:
                fids.add(x.get('fid1'))
                fids.add(x.get('fid2'))
                contributing+=1
        regions.append({
            'geometry':part,
            'area':part.area(),
            'source_fids':sorted(f for f in fids if f is not None),
            'overlap_count':contributing,
        })
    return regions

# ---------------------------------------------------------------------------
# Canonical overlap detector -- shared by the Overlap Areas report AND the
# overlap-removal repair step, so both stages agree on exactly what counts
# as an overlap. This mirrors how QGIS's own Topology Checker core plugin
# tests its "Must not overlap" rule: bounding-box prefilter via a spatial
# index, then an exact GEOS intersects()/intersection() test, keeping only
# pairs whose intersection has positive polygon area (a shared boundary or
# a touching edge, with zero-area intersection, is NOT an overlap).
# ---------------------------------------------------------------------------

def detect_overlaps(geoms,order=None,feedback=None,area_tolerance=0.0):
    """Topology-Checker-style overlap scan over a {fid: geometry} mapping.

    order, if given, is a list of fids defining priority (lowest index =
    lowest priority). Each returned pair is reported as (fid1, fid2) with
    fid1 always the earlier/lower-priority feature, so callers can
    consistently decide which side of the pair to trim. If order is
    omitted, dict insertion order is used.

    Returns a list of dicts: {'fid1','fid2','area','geometry'} -- the same
    shape find_overlaps() has always returned.
    """
    fids=order if order is not None else list(geoms.keys())
    pos={fid:i for i,fid in enumerate(fids)}
    idx=QgsSpatialIndex()
    for fid in fids:
        g=geoms.get(fid)
        if g is not None and not g.isEmpty():
            feat=QgsFeature(); feat.setId(fid); feat.setGeometry(g)
            idx.insertFeature(feat)
    results=[]; seen=set(); total=len(fids) or 1
    for i,fid in enumerate(fids):
        g=geoms.get(fid)
        if g is None or g.isEmpty():
            if feedback: feedback.setProgress(int(100*(i+1)/total))
            continue
        for cid in idx.intersects(g.boundingBox()):
            if cid==fid or cid not in pos: continue
            a_id,b_id=(fid,cid) if pos[fid]<pos[cid] else (cid,fid)
            pair=(a_id,b_id)
            if pair in seen: continue
            seen.add(pair)
            g1=geoms.get(a_id); g2=geoms.get(b_id)
            if g1 is None or g2 is None or g1.isEmpty() or g2.isEmpty(): continue
            try:
                if not g1.intersects(g2): continue
                inter=g1.intersection(g2)
            except Exception:
                continue
            if inter is None or inter.isEmpty(): continue
            try: is_area=QgsWkbTypes.geometryType(inter.wkbType())==QgsWkbTypes.PolygonGeometry
            except Exception: is_area=False
            area=inter.area() if is_area else 0.0
            if area>area_tolerance:
                results.append({'fid1':a_id,'fid2':b_id,'area':area,'geometry':inter})
        if feedback: feedback.setProgress(int(100*(i+1)/total))
    return results

# A single, deliberately small precision model is used for ALL geometries
# participating in overlap repair.  The important point is not the absolute
# value of the grid, but that source polygons, blockers and repaired results
# are represented on the SAME grid.  Snapping only the result of difference()
# can otherwise leave two logically shared edges with different coordinates.
_OVERLAP_GRID_SIZE=1e-9

def _snap_to_grid(g,size=_OVERLAP_GRID_SIZE):
    """Return a copy of *g* on the common GeoMend precision grid.

    This is a precision normalisation step, not an overlap tolerance.  It is
    intentionally small and is applied consistently to every polygon used by
    the overlap overlay operations.
    """
    if g is None or g.isEmpty(): return g
    try:
        snapped=g.snappedToGrid(size,size)
        if snapped is not None and not snapped.isEmpty():
            return snapped
    except Exception:
        pass
    return QgsGeometry(g)

def _normalise_polygon_geometries(geometries,size=_OVERLAP_GRID_SIZE):
    """Put every working polygon on the same precision grid.

    This must be done BEFORE overlay operations, not only after them.  It
    prevents an unsnapped blocker from creating a boundary which differs by a
    few floating-point units from the subsequently snapped result.
    """
    out={}
    for fid,g in geometries.items():
        if g is None or g.isEmpty():
            out[fid]=QgsGeometry()
            continue
        ng=_snap_to_grid(g,size)
        if ng is None or ng.isEmpty():
            out[fid]=QgsGeometry()
            continue
        if QgsWkbTypes.geometryType(ng.wkbType())!=QgsWkbTypes.PolygonGeometry:
            ng=_extract_polygons(ng)
        if ng is None or ng.isEmpty():
            out[fid]=QgsGeometry()
            continue
        try:
            if not ng.isGeosValid():
                fixed,_,bad=fix_invalid_geometry(ng)
                if fixed is not None and not fixed.isEmpty() and not bad:
                    ng=_snap_to_grid(fixed,size)
        except Exception:
            pass
        out[fid]=ng
    return out

def _clean_polygon_result(g):
    """Clean an overlay result without introducing a different precision model.

    If makeValid() changes the geometry, the repaired geometry is snapped back
    to the SAME common grid before it is returned.
    """
    if g is None or g.isEmpty(): return QgsGeometry()
    g=_snap_to_grid(g)
    if g is None or g.isEmpty(): return QgsGeometry()
    if QgsWkbTypes.geometryType(g.wkbType())!=QgsWkbTypes.PolygonGeometry:
        g=_extract_polygons(g)
        if g is None or g.isEmpty(): return QgsGeometry()
    try:
        valid=g.isGeosValid()
    except Exception:
        valid=True
    if not valid:
        fixed,_,_=fix_invalid_geometry(g)
        if fixed is not None and not fixed.isEmpty():
            g=_snap_to_grid(fixed)
            if QgsWkbTypes.geometryType(g.wkbType())!=QgsWkbTypes.PolygonGeometry:
                g=_extract_polygons(g)
    return g

def find_overlaps(layer,geometries=None,feedback=None,feature_ids=None):
    if QgsWkbTypes.geometryType(layer.wkbType())!=QgsWkbTypes.PolygonGeometry: return []
    allowed=set(feature_ids) if feature_ids is not None else None
    geoms=geometries or {f.id():f.geometry() for f in layer.getFeatures() if allowed is None or f.id() in allowed}
    return detect_overlaps(geoms,feedback=feedback)

def polygon_layers_underneath(selected):
    """Return polygon layers below *selected* in the visible QGIS layer-tree order.
    Handles layers inside groups as well as top-level layers.
    """
    root = QgsProject.instance().layerTreeRoot()
    nodes = []
    def walk(parent):
        for child in parent.children():
            if hasattr(child, 'layerId') and child.layerId():
                nodes.append(child)
            elif hasattr(child, 'children'):
                walk(child)
    walk(root)
    selected_id = selected.id()
    pos = next((i for i, n in enumerate(nodes) if n.layerId() == selected_id), None)
    if pos is None:
        return []
    result = []
    project = QgsProject.instance()
    for n in nodes[pos + 1:]:
        l = project.mapLayer(n.layerId())
        if isinstance(l, QgsVectorLayer) and l.isValid() and QgsWkbTypes.geometryType(l.wkbType()) == QgsWkbTypes.PolygonGeometry:
            result.append(l)
    return result


def _safe_union(geometries):
    usable = [QgsGeometry(g) for g in geometries if g and not g.isEmpty()]
    if not usable:
        return None
    try:
        u = QgsGeometry.unaryUnion(usable)
    except Exception:
        u = None
    if u is None or u.isEmpty():
        u = usable[0]
        for g in usable[1:]:
            try:
                u = u.combine(g)
            except Exception:
                pass
    return u if u and not u.isEmpty() else None


def cut_overlap_with_underlying(selected, geometries, under_layers, feedback=None):
    """Cut the overlap out of the selected/top layer only.
    Every polygon layer underneath has priority and is left untouched.
    Source attributes are retained by the caller when the output is built.
    """
    blockers = []
    for layer in under_layers:
        for f in layer.getFeatures():
            g = f.geometry()
            if g and not g.isEmpty():
                blockers.append(g)
    # Use the same precision model for blockers and the working output.  An
    # unsnapped underlying polygon combined with a snapped result can otherwise
    # create a microscopic mismatch along the newly shared boundary.
    blockers=[_snap_to_grid(g) for g in blockers if g and not g.isEmpty()]
    block_union = _safe_union(blockers)
    if block_union is None:
        return dict(geometries), 0, 0.0

    result = {}
    changed = 0
    removed_area = 0.0
    total = len(geometries) or 1
    for i, (fid, g) in enumerate(geometries.items()):
        if g is None or g.isEmpty():
            result[fid] = g
            continue
        original = QgsGeometry(g)
        try:
            ng = original.difference(block_union)
        except Exception:
            # Per-feature fallback prevents one GEOS failure from producing an empty result.
            ng = QgsGeometry(original)
            for blocker in blockers:
                try:
                    ng = ng.difference(blocker)
                    if ng.isEmpty():
                        break
                except Exception:
                    continue
        ng = _clean_polygon_result(ng)
        try:
            removed = max(0.0, original.area() - (ng.area() if not ng.isEmpty() else 0.0))
        except Exception:
            removed = 0.0
        if removed > 0:
            changed += 1
            removed_area += removed
        result[fid] = ng
        if feedback:
            feedback.setProgress(int(100 * (i + 1) / total))
    result=_normalise_polygon_geometries(result)
    return result, changed, removed_area

def convert_distance(value,unit,crs):
    if value<=0:return 0.0
    if crs.mapUnits()==QgsUnitTypes.DistanceDegrees: raise ValueError('The selected layer uses a geographic CRS (degrees). Reproject it to a suitable projected CRS before using metre/foot operations.')
    source=QgsUnitTypes.DistanceMeters if unit=='Meters' else QgsUnitTypes.DistanceFeet
    return QgsUnitTypes.fromUnitToUnitFactor(source,crs.mapUnits())*value

def snap_layer_to_underlying(selected,geometries,tolerance_map_units,under_layers):
    if not under_layers: return dict(geometries),False,'No polygon layer exists underneath the selected layer.'
    refs=[]
    for l in under_layers:
        for f in l.getFeatures():
            if f.geometry() and not f.geometry().isEmpty(): refs.append(f.geometry())
    if not refs:return dict(geometries),False,'Underlying polygon layers contain no usable geometries.'
    try:
        import processing
        ref=QgsGeometry.unaryUnion(refs)
        ref_layer=QgsVectorLayer('Polygon?crs='+selected.crs().authid(),'_snap_reference','memory')
        ref_layer.dataProvider().addAttributes([QgsField('id',QVariant.Int)]); ref_layer.updateFields()
        nf=QgsFeature(ref_layer.fields()); nf.setGeometry(ref); nf.setAttribute('id',1); ref_layer.dataProvider().addFeature(nf); ref_layer.updateExtents()
        inp=QgsVectorLayer('Polygon?crs='+selected.crs().authid(),'_snap_input','memory'); inp.dataProvider().addAttributes(list(selected.fields())); inp.updateFields()
        for f in selected.getFeatures():
            g=geometries.get(f.id())
            if g and not g.isEmpty():
                x=QgsFeature(inp.fields()); x.setGeometry(g); x.setAttributes(f.attributes()); inp.dataProvider().addFeature(x)
        inp.updateExtents(); out=processing.run('native:snapgeometries',{'INPUT':inp,'REFERENCE_LAYER':ref_layer,'TOLERANCE':tolerance_map_units,'BEHAVIOR':0,'OUTPUT':'memory:'})['OUTPUT']
        result=dict(geometries)
        for src,nf in zip([f for f in selected.getFeatures() if geometries.get(f.id()) and not geometries.get(f.id()).isEmpty()],out.getFeatures()): result[src.id()]=nf.geometry()
        return result,True,''
    except Exception as e: return dict(geometries),False,str(e)

# ===========================================================================
# GAP ELIMINATION ENGINE
# ===========================================================================
#
# Workflow (per iteration):
#   1. Build the current working coverage (editable features + immutable
#      unselected context features).
#   2. Detect the ACTUAL gap geometries between separate polygon features:
#        * enclosed gaps  -> interior rings of the dissolved coverage that are
#                            bounded by two or more DIFFERENT features
#                            (holes bounded by a single feature are legitimate
#                            holes and are never touched);
#        * open gaps      -> narrow channels that are open at one/both ends,
#                            found by a morphological closing of the coverage.
#                            The closing is used ONLY to locate the gap; the
#                            geometry that is filled is (closing - coverage),
#                            i.e. exactly the empty space, so it can never
#                            overlap a polygon.
#   3. Measure each gap's true width (largest inscribed circle diameter, so a
#      big gap with one narrow neck does NOT qualify) and its neighbours.
#   4. Assign each qualifying gap to ONE receiving feature deterministically:
#      longest shared boundary wins, lowest FID breaks ties.  Only editable
#      (selected) features may receive a gap.
#   5. Union the gap into the receiver, then VERIFY (valid geometry, gap fully
#      covered, only the gap area added, no overlap with any other polygon).
#      Only a verified fill is accepted and reported as corrected=True.
#   6. Repeat until nothing more can be corrected (max GAP_MAX_ITERATIONS).
#   7. A final, independent topology check (verify_topology) reports remaining
#      gaps, overlaps and invalid geometries from the real final geometries.

import math

GAP_MAX_ITERATIONS=10
DEFAULT_GAP_TOLERANCE_M=0.5
# An open-ended gap must be an elongated channel between two features: each of
# (at least) two neighbours must share >= this many tolerances of boundary with
# it.  This stops tiny notches at the outer edge of the coverage from being
# treated as "gaps between neighbouring polygons".
_OPEN_GAP_MIN_CONTACT_FACTOR=2.0
# Closing radius = tolerance/2 (plus a hair) so a gap of exactly the
# tolerance is still bridged.  The taper guard re-runs the closing with a
# radius this much larger to detect a "gap" that is really just the narrow
# end of a wider wedge.
_GAP_RADIUS_SAFETY=1.001
# Detection reaches gaps up to 2x the tolerance wide so that long tapering
# slivers (wide at one end, ~0 at the other) are seen as ONE complete gap.
# Whether such a gap is then corrected is decided by _classify_gap().
_GAP_TAPER_GROWTH=0.25
_GAP_BUFFER_SEGMENTS=64   # arc smoothness of the closing (higher = smaller residual)


def _line_length(g):
    """Total length of the linear parts of *g* (robust to collections)."""
    if g is None or g.isEmpty():
        return 0.0
    total=0.0
    parts=g.asGeometryCollection() if g.isMultipart() else [g]
    for p in parts:
        try:
            if QgsWkbTypes.geometryType(p.wkbType())==QgsWkbTypes.LineGeometry:
                total+=p.length()
        except Exception:
            continue
    return total


def _area_of(g):
    """Polygon area of *g* (0 if it is not / does not contain polygons)."""
    if g is None or g.isEmpty():
        return 0.0
    try:
        if QgsWkbTypes.geometryType(g.wkbType())==QgsWkbTypes.PolygonGeometry:
            return g.area()
        polys=_extract_polygons(g)
        return polys.area() if polys is not None and not polys.isEmpty() else 0.0
    except Exception:
        return 0.0


def _gap_thresholds(tol):
    """(precision for contact tests, minimum polygon area worth considering,
    area tolerance for verification)."""
    prec=max(tol*1e-5,1e-8)
    min_area=tol*tol*1e-6
    eps_area=(tol*1e-3)**2
    return prec,min_area,eps_area


def _gap_max_width(gap,tol):
    """Width of a gap = diameter of the largest circle that fits inside it.

    This is a true "widest point" measure: a large gap with one narrow neck has
    a large inscribed circle and therefore does NOT qualify.  (The old
    minimum-width/convex-hull measure could under-report L-shaped or curved
    gaps.)  Precision is tol/200, i.e. the width is accurate to ~1 %.
    """
    try:
        res=gap.poleOfInaccessibility(max(tol/200.0,1e-9))
        dist=res[1] if isinstance(res,(tuple,list)) else None
        if dist is not None:
            return 2.0*float(dist)
    except Exception:
        pass
    try:
        mw=gap.minimumWidth()
        if mw and not mw.isEmpty():
            return mw.length()
    except Exception:
        pass
    return float('inf')


def _perimeter(g):
    try: return _line_length(g.boundary())
    except Exception: return 0.0


def _gap_measures(gap,tol):
    """(max_width, mean_width).  max = diameter of the largest inscribed
    circle; mean = 2*area/perimeter, the average width of a long sliver."""
    mx=_gap_max_width(gap,tol)
    per=_perimeter(gap)
    mean=(2.0*gap.area()/per) if per>0 else float('inf')
    return mx,mean


def _classify_gap(mx,mean,tol,allow_slivers):
    """(qualifies, reason).

    Strict rule: the WIDEST point of the gap is <= tolerance.
    Sliver rule (optional): the gap is a long tapering sliver whose AVERAGE
    width is <= tolerance and whose widest point is <= 2x tolerance.  A large
    gap with one narrow neck has a large average width and never qualifies.
    """
    if mx<=tol*(1.0+1e-3):
        return True,''
    if allow_slivers and mean<=tol and mx<=2.0*tol*(1.0+1e-3):
        return True,''
    return False,'wider than gap tolerance'


def _gap_key(gap):
    try:
        c=gap.centroid().asPoint()
        return (round(gap.area(),6),round(c.x(),3),round(c.y(),3))
    except Exception:
        return (round(gap.area(),6),0.0,0.0)


def _gap_contacts(gap,polys,idx,prec):
    """{fid: shared-boundary length} between *gap* and every polygon that
    actually touches it along a line (a point touch does not count)."""
    out={}
    try:
        bb=QgsRectangle(gap.boundingBox()); bb.grow(max(prec*100.0,1e-7))
        box=QgsGeometry.fromRect(bb)
        bound=gap.boundary()
        for fid in idx.intersects(bb):
            g=polys.get(fid)
            if g is None or g.isEmpty():
                continue
            try:
                edge=g.boundary().intersection(box)
                if edge is None or edge.isEmpty():
                    continue
                shared=bound.intersection(edge.buffer(prec,1))
                length=_line_length(shared)
            except Exception:
                continue
            if length>prec*10.0:
                out[fid]=length
    except Exception:
        pass
    return out


def _complete_mouth_caps(fill,base,r,prec):
    """Complete the rounded ends of a closing-derived gap.

    A morphological closing leaves a small scalloped pocket at each open end of
    a narrow channel.  Left alone, that pocket would remain as a permanent
    notch.  Each free (non-polygon) boundary line of the fill is replaced by
    its convex hull -- a circular segment spanning the two polygon corners --
    so the gap ends flush at the mouth.  The added area is always taken from
    the empty space (hull - coverage) and is capped, so it cannot overlap a
    polygon or grow unreasonably.
    """
    try:
        bb=QgsRectangle(fill.boundingBox()); bb.grow(max(r*4.0,prec*100.0))
        local_base=base.intersection(QgsGeometry.fromRect(bb))
        free=fill.boundary().difference(local_base.buffer(prec*2.0,2))
    except Exception:
        return fill
    if free is None or free.isEmpty():
        return fill
    pieces=[fill]
    cap_limit=2.0*math.pi*r*r*1.5
    lines=free.asGeometryCollection() if free.isMultipart() else [free]
    for ln in lines:
        try:
            if QgsWkbTypes.geometryType(ln.wkbType())!=QgsWkbTypes.LineGeometry:
                continue
            if ln.length()<=prec*100.0:
                continue
            # Pin both ends of the arc exactly onto the polygon boundary so
            # no sliver is left between the arc end and the polygon corner.
            hull_src=[ln]
            try:
                pl=ln.asPolyline()
                for p in (pl[0],pl[-1]):
                    pg=QgsGeometry.fromPointXY(QgsPointXY(p.x(),p.y()))
                    pin=None
                    # Prefer the actual polygon corner if one is very close:
                    # the gap then ends exactly at the polygon vertex.
                    best=None
                    for v in local_base.vertices():
                        d=(v.x()-p.x())**2+(v.y()-p.y())**2
                        if best is None or d<best[0]: best=(d,v)
                    if best is not None and best[0]<=(r*0.5)**2:
                        pin=QgsGeometry.fromPointXY(QgsPointXY(best[1].x(),best[1].y()))
                    if pin is None:
                        pin=local_base.nearestPoint(pg)
                    if pin is not None and not pin.isEmpty():
                        hull_src.append(pin)
            except Exception:
                pass
            hull=QgsGeometry.unaryUnion(hull_src).convexHull()
            if hull is None or hull.isEmpty() or QgsWkbTypes.geometryType(hull.wkbType())!=QgsWkbTypes.PolygonGeometry:
                continue
            lens=hull.difference(local_base)
            if lens is None or lens.isEmpty():
                continue
            if _area_of(lens)>cap_limit:
                continue
            pieces.append(lens)
        except Exception:
            continue
    merged=_safe_union(pieces)
    if merged is None or merged.isEmpty():
        return fill
    try:
        merged=merged.difference(local_base)
    except Exception:
        return fill
    poly=_extract_polygons(merged) if QgsWkbTypes.geometryType(merged.wkbType())!=QgsWkbTypes.PolygonGeometry else merged
    return poly if poly is not None and not poly.isEmpty() else fill


def _detect_gaps(polys,tol,allowed=None,allow_slivers=True):
    """Detect the actual gap geometries between polygon features.

    *polys* is {fid: polygon geometry} for the whole coverage (editable and
    immutable features together).  Returns a list of dicts:
      geometry, area, width, kind ('enclosed'|'open'), neighbor_fids,
      contacts, qualifies (bool), eligible ({fid: contact} of receivers),
      reason (why it does not qualify / cannot be corrected).
    Legitimate holes (bounded by one feature only) are NOT returned.
    """
    prec,min_area,eps_area=_gap_thresholds(tol)
    usable={fid:g for fid,g in polys.items() if g is not None and not g.isEmpty()}
    if len(usable)<1 or tol<=0:
        return []
    union=_safe_union(list(usable.values()))
    if union is None or union.isEmpty():
        return []

    idx=QgsSpatialIndex()
    for fid,g in usable.items():
        f=QgsFeature(); f.setId(fid); f.setGeometry(g); idx.insertFeature(f)

    # -- coverage without its interior rings ("filled" coverage) ------------
    holes=[]; filled_parts=[]
    for part in _explode_to_singlepart_polygons(union):
        try:
            rings=part.asPolygon()
        except Exception:
            rings=[]
        if not rings:
            filled_parts.append(part); continue
        filled_parts.append(QgsGeometry.fromPolygonXY([rings[0]]))
        for ring in rings[1:]:
            if len(ring)>=4:
                h=QgsGeometry.fromPolygonXY([ring])
                if h is None or h.isEmpty():
                    continue
                # Another dissolved part may sit INSIDE this ring (an island);
                # the real empty space is the ring minus the coverage.
                try: h=h.difference(union)
                except Exception: pass
                for hp in _explode_to_singlepart_polygons(h):
                    if hp.area()>min_area:
                        holes.append(hp)

    gaps=[]

    # -- enclosed gaps ------------------------------------------------------
    for h in holes:
        contacts=_gap_contacts(h,usable,idx,prec)
        if len(contacts)<2:
            continue   # legitimate hole of a single polygon: never a gap
        width,mean=_gap_measures(h,tol)
        ok,reason=_classify_gap(width,mean,tol,allow_slivers)
        gaps.append({'geometry':h,'area':h.area(),'width':width,'mean_width':mean,'kind':'enclosed',
                     'contacts':contacts,'neighbor_fids':sorted(contacts.keys()),
                     'qualifies':ok,'reason':reason})

    # -- open (non-enclosed) narrow gaps ------------------------------------
    base=_safe_union(filled_parts)
    r=tol*_GAP_RADIUS_SAFETY   # closing radius: bridges gaps up to ~2x tolerance
    open_parts=[]
    try:
        closed=base.buffer(r,_GAP_BUFFER_SEGMENTS).buffer(-r,_GAP_BUFFER_SEGMENTS)
        fill=closed.difference(base)
        open_parts=[p for p in _explode_to_singlepart_polygons(fill) if p.area()>min_area]
    except Exception:
        open_parts=[]

    if open_parts:
        # Taper guard: refuse the narrow tip of a wider wedge.
        taper_bad=set()
        try:
            r2=r*(1.0+_GAP_TAPER_GROWTH)
            fill2=base.buffer(r2,_GAP_BUFFER_SEGMENTS).buffer(-r2,_GAP_BUFFER_SEGMENTS).difference(base)
            parts2=[p for p in _explode_to_singlepart_polygons(fill2) if p.area()>min_area]
            cap=2.0*math.pi*r2*r2
            for p2 in parts2:
                inside=[i for i,p1 in enumerate(open_parts)
                        if p1.boundingBox().intersects(p2.boundingBox()) and p1.intersects(p2)]
                if not inside:
                    continue
                total1=sum(open_parts[i].area() for i in inside)
                if p2.area()-total1>cap*len(inside)+0.02*total1:
                    taper_bad.update(inside)
        except Exception:
            pass

        min_contact=tol*_OPEN_GAP_MIN_CONTACT_FACTOR
        for i,p1 in enumerate(open_parts):
            gap=_complete_mouth_caps(p1,base,r,prec)
            if gap is None or gap.isEmpty():
                continue
            contacts=_gap_contacts(gap,usable,idx,prec)
            long_enough=[fid for fid,L in contacts.items() if L>=min_contact]
            if len(long_enough)<2:
                continue   # notch / corner artefact, not a gap between neighbours
            width,mean=_gap_measures(gap,tol)
            if i in taper_bad:
                ok=False; reason='wider than gap tolerance (narrow end of a wider gap)'
            else:
                ok,reason=_classify_gap(width,mean,tol,allow_slivers)
            gaps.append({'geometry':gap,'area':gap.area(),'width':width,'mean_width':mean,'kind':'open',
                         'contacts':contacts,'neighbor_fids':sorted(contacts.keys()),
                         'qualifies':ok,'reason':reason})

    # -- receivers -----------------------------------------------------------
    for gp in gaps:
        el={fid:L for fid,L in gp['contacts'].items() if allowed is None or fid in allowed}
        gp['eligible']=el
        if gp['qualifies'] and not el:
            gp['reason']='no editable (selected) neighbouring polygon'
    return gaps


def _choose_receiver(eligible):
    """Deterministic receiver: longest shared boundary; lowest FID on ties.
    Lengths are rounded so floating-point noise cannot flip the decision."""
    ranked=sorted(eligible.items(),key=lambda kv:(-round(kv[1],6),kv[0]))
    return ranked[0][0] if ranked else None


def _verify_fill(before,after,gap,others,tol):
    """Return (ok, reason).  Checks the receiving polygon after the union."""
    prec,min_area,eps_area=_gap_thresholds(tol)
    eps=max(eps_area,gap.area()*1e-6)
    if after is None or after.isEmpty():
        return False,'union produced an empty geometry'
    if QgsWkbTypes.geometryType(after.wkbType())!=QgsWkbTypes.PolygonGeometry:
        return False,'union did not produce a polygon'
    try:
        if not after.isGeosValid():
            return False,'receiving polygon invalid after union'
    except Exception:
        pass
    try:
        if _area_of(gap.difference(after))>eps:
            return False,'gap not fully covered by receiving polygon'
        added=after.difference(before)
        added_area=_area_of(added)
        if added_area<=eps_area:
            return False,'no area was added'
        if _area_of(added.difference(gap))>eps:
            return False,'union added area outside the gap'
        for og in others:
            if og is None or og.isEmpty():
                continue
            if gap.boundingBox().intersects(og.boundingBox()) and _area_of(gap.intersection(og))>eps_area:
                return False,'fill would overlap a neighbouring polygon'
    except Exception as e:
        return False,'verification error: %s'%e
    return True,''


def eliminate_small_gaps(geometries,tolerance,allowed_fids=None,context_geometries=None,
                         feedback=None,max_iterations=GAP_MAX_ITERATIONS,allow_slivers=True):
    """Physically incorporate qualifying small gaps into neighbouring polygons.

    geometries        {fid: geometry} working (output) features
    tolerance         max gap width in map units
    allowed_fids      fids that may receive a gap (None = all of *geometries*)
    context_geometries {fid: geometry} immutable features (e.g. unselected
                      features) that bound gaps but are never modified
    Returns dict: geometries, corrected_count, gaps (records with gap_id, area,
    width, neighbor_fids, corrected, target_fid, ...), added_area (per fid),
    iterations, max_iterations_reached.
    """
    result=_normalise_polygon_geometries({fid:(QgsGeometry(g) if g is not None else QgsGeometry())
                                          for fid,g in geometries.items()})
    ctx={}
    for fid,g in (context_geometries or {}).items():
        if fid in result or g is None or g.isEmpty():
            continue
        ctx[fid]=g
    ctx=_normalise_polygon_geometries(ctx)
    allowed=set(result.keys()) if allowed_fids is None else (set(allowed_fids)&set(result.keys()))
    tol=float(tolerance or 0.0)
    out={'geometries':result,'corrected_count':0,'gaps':[],'added_area':{},
         'iterations':0,'max_iterations_reached':False}
    if tol<=0:
        return out

    corrected=[]; fail_reason={}
    iterations=0
    for it in range(1,max_iterations+1):
        iterations=it
        merged=dict(ctx); merged.update(result)
        gaps=_detect_gaps(merged,tol,allowed,allow_slivers)
        todo=[g for g in gaps if g['qualifies'] and g['eligible']]
        if not todo:
            break
        idx=QgsSpatialIndex()
        for fid,g in merged.items():
            if g is not None and not g.isEmpty():
                f=QgsFeature(); f.setId(fid); f.setGeometry(g); idx.insertFeature(f)
        snapshot=merged
        n_ok=0
        # Deterministic processing order (largest gap first, then position).
        todo.sort(key=lambda gp:(-round(gp['area'],9),_gap_key(gp['geometry'])))
        for n,gp in enumerate(todo,1):
            gap=gp['geometry']
            target=_choose_receiver(gp['eligible'])
            before=result[target]
            try:
                after=before.combine(gap)
            except Exception:
                try: after=QgsGeometry.unaryUnion([before,gap])
                except Exception: after=None
            after=_clean_polygon_result(after) if after is not None else None
            others=[snapshot[fid] for fid in idx.intersects(gap.boundingBox()) if fid!=target]
            ok,reason=_verify_fill(before,after,gap,others,tol)
            if not ok:
                fail_reason[_gap_key(gap)]=reason
                continue
            added=max(0.0,_area_of(after)-_area_of(before))
            result[target]=after
            out['added_area'][target]=out['added_area'].get(target,0.0)+added
            n_ok+=1
            corrected.append({'geometry':gap,'area':gp['area'],'width':gp['width'],'mean_width':gp.get('mean_width'),
                              'kind':gp['kind'],'neighbor_fids':gp['neighbor_fids'],
                              'corrected':True,'target_fid':target,'reason':'',
                              'iteration':it})
            if feedback:
                feedback.setProgress(int(100*n/(len(todo) or 1)))
        result=_normalise_polygon_geometries(result)
        if n_ok==0:
            break

    # Final detection pass: anything still present is reported honestly.
    merged=dict(ctx); merged.update(result)
    final=_detect_gaps(merged,tol,allowed,allow_slivers)
    remaining=[]
    pending=0
    for gp in final:
        if gp['qualifies'] and gp['eligible']:
            pending+=1
            reason=fail_reason.get(_gap_key(gp['geometry']),'not corrected')
        else:
            reason=gp['reason']
        remaining.append({'geometry':gp['geometry'],'area':gp['area'],'width':gp['width'],'mean_width':gp.get('mean_width'),
                          'kind':gp['kind'],'neighbor_fids':gp['neighbor_fids'],
                          'corrected':False,'target_fid':None,'reason':reason,
                          'iteration':iterations})
    out['max_iterations_reached']=bool(iterations>=max_iterations and pending>0)
    records=corrected+remaining
    for i,rec in enumerate(records,1):
        rec['gap_id']=i
    out.update({'geometries':result,'corrected_count':len(corrected),'gaps':records,
                'iterations':iterations})
    return out


def analyse_gaps(geometries,tolerance,allowed_fids=None,context_geometries=None,allow_slivers=True):
    """Detect (never modify) the gaps of a coverage.  Same record shape as
    eliminate_small_gaps()['gaps'] with corrected=False for every record.
    Legitimate single-feature holes are not reported."""
    working=_normalise_polygon_geometries({fid:g for fid,g in geometries.items()
                                           if g is not None and not g.isEmpty()})
    merged=dict(context_geometries or {}); merged.update(working)
    allowed=None if allowed_fids is None else set(allowed_fids)
    gaps=_detect_gaps(merged,float(tolerance or 0.0),allowed,allow_slivers)
    records=[]
    for i,gp in enumerate(gaps,1):
        records.append({'gap_id':i,'geometry':gp['geometry'],'area':gp['area'],'width':gp['width'],'mean_width':gp.get('mean_width'),
                        'kind':gp['kind'],'neighbor_fids':gp['neighbor_fids'],
                        'qualifies':gp['qualifies'],'corrected':False,'target_fid':None,
                        'reason':gp['reason']})
    return records


def find_gaps(layer,feedback=None,feature_ids=None,tolerance=None,allow_slivers=True):
    """Layer-based gap detection for reporting (does not modify anything).
    Features outside *feature_ids* still bound gaps as immutable context."""
    if QgsWkbTypes.geometryType(layer.wkbType())!=QgsWkbTypes.PolygonGeometry or not tolerance:
        return []
    editable={}; context={}
    for f in layer.getFeatures():
        g=f.geometry()
        if g is None or g.isEmpty():
            continue
        g=QgsGeometry(g)
        if not g.isGeosValid():
            fixed,_,_=fix_invalid_geometry(g); g=fixed if fixed is not None else g
        (editable if (feature_ids is None or f.id() in feature_ids) else context)[f.id()]=g
    return analyse_gaps(editable,tolerance,allowed_fids=set(editable.keys()),context_geometries=context,allow_slivers=allow_slivers)


def verify_topology(geometries,tolerance,context_geometries=None,allowed_fids=None,noise_tol=None,allow_slivers=True):
    """Independent final check of the REAL final geometries.

    Reports remaining qualifying gaps, retained wider gaps, positive-area
    overlaps and invalid geometries.  'clean' is True only when there is no
    remaining qualifying gap, no overlap and no invalid geometry.
    """
    tol=float(tolerance or 0.0)
    prec,min_area,eps_area=_gap_thresholds(noise_tol if noise_tol else (tol if tol>0 else 1.0))
    merged={fid:g for fid,g in (context_geometries or {}).items() if g is not None and not g.isEmpty()}
    for fid,g in geometries.items():
        if g is not None and not g.isEmpty():
            merged[fid]=g
    allowed=None if allowed_fids is None else set(allowed_fids)
    gaps=_detect_gaps(merged,tol,allowed,allow_slivers) if tol>0 else []
    qualifying=[g for g in gaps if g['qualifies'] and g['eligible']]
    wider=[g for g in gaps if not g['qualifies'] and g['reason'].startswith('wider')]
    out_scope=[g for g in gaps if g['qualifies'] and not g['eligible']]
    overlaps=detect_overlaps(merged,area_tolerance=eps_area)
    invalid=0
    for g in merged.values():
        try:
            if not g.isGeosValid(): invalid+=1
        except Exception:
            invalid+=1
    ctx_ids=set(context_geometries or {})
    ov_ctx=[o for o in overlaps if o['fid1'] in ctx_ids or o['fid2'] in ctx_ids]
    return {'remaining_qualifying_gaps':len(qualifying),
            'retained_wider_gaps':len(wider),
            'out_of_scope_gaps':len(out_scope),
            'overlap_count':len(overlaps),
            'overlap_area':sum(o['area'] for o in overlaps),
            'overlaps_with_unselected':len(ov_ctx),
            'invalid_geometries':invalid,
            'clean':(not qualifying and not overlaps and invalid==0)}


def sequential_inward_buffers(layer,distances,output_names=None,remainder_name='inward_buffer_remainder',combined_name='inward_buffer_combined',feature_ids=None):
    """Generate exactly len(distances) buffer section layers, one remainder layer, and one combined layer."""
    names=list(output_names or [])[:5]
    defaults=[f'inward_buffer_{i}' for i in range(1,6)]
    names += defaults[len(names):5]
    allowed=set(feature_ids) if feature_ids is not None else None
    source_features=[f for f in layer.getFeatures() if allowed is None or f.id() in allowed]
    active={f.id():QgsGeometry(f.geometry()) for f in source_features}
    outputs=[]
    for section,d in enumerate(distances,1):
        ring_name=names[section-1]
        ring=_make_buffer_layer(layer,ring_name)
        feats=[]; nxt={}
        for f in source_features:
            core=active.get(f.id())
            if core is None or core.isEmpty(): continue
            try: inner=core.buffer(-d,16)
            except Exception: inner=None
            if inner is None or inner.isEmpty():
                ringgeom=core; nxt[f.id()]=QgsGeometry()
            else:
                ringgeom=core.difference(inner); nxt[f.id()]=inner
            if ringgeom and not ringgeom.isEmpty():
                nf=QgsFeature(ring.fields()); nf.setGeometry(ringgeom); nf.setAttributes(list(f.attributes())+[f.id(),section,d]); feats.append(nf)
        ring.dataProvider().addFeatures(feats); ring.updateExtents(); outputs.append(ring); active=nxt
    rem=_make_buffer_layer(layer,remainder_name)
    feats=[]
    for f in source_features:
        g=active.get(f.id())
        if g and not g.isEmpty():
            nf=QgsFeature(rem.fields()); nf.setGeometry(g); nf.setAttributes(list(f.attributes())+[f.id(),0,0.0]); feats.append(nf)
    rem.dataProvider().addFeatures(feats); rem.updateExtents(); outputs.append(rem)
    combined=_make_buffer_layer(layer,combined_name)
    combined.dataProvider().addAttributes([QgsField('source_layer',QVariant.String)]); combined.updateFields(); all_feats=[]
    for l in outputs:
        for f in l.getFeatures():
            nf=QgsFeature(combined.fields()); nf.setGeometry(f.geometry()); nf.setAttributes(list(f.attributes())+[l.name()]); all_feats.append(nf)
    combined.dataProvider().addFeatures(all_feats); combined.updateExtents(); outputs.append(combined)
    # NOTE: none of these layers are added to the project here. The caller
    # (GeometryRepairPlugin, in geometry_repair.py) is responsible for that,
    # via _finalize_layer(), so the user's Output Destination choice
    # (temporary layer vs. save to disk) is honoured uniformly for every
    # layer this plugin produces, in every tab.
    return outputs

def _make_buffer_layer(source,name):
    out=QgsVectorLayer(f'Polygon?crs={source.crs().authid()}',name,'memory')
    p=out.dataProvider(); fields=[source.fields().at(i) for i in range(source.fields().count())]
    fields += [QgsField('source_fid',QVariant.Int),QgsField('section',QVariant.Int),QgsField('distance',QVariant.Double)]
    p.addAttributes(fields); out.updateFields(); return out


# ---------------------------------------------------------------------------
# Duplicate detection / removal
# ---------------------------------------------------------------------------

def find_duplicate_geometries(layer,feedback=None,feature_ids=None):
    """Return the fids of features whose geometry exactly duplicates a feature
    already seen earlier in the layer. The first occurrence of a shape is kept;
    every later exact copy is reported as a duplicate.
    """
    seen=[]; duplicates=[]; idx=QgsSpatialIndex(); allowed=set(feature_ids) if feature_ids is not None else None
    feats=[f for f in layer.getFeatures() if allowed is None or f.id() in allowed]
    lookup={f.id():f for f in feats}
    for f in feats:
        g=f.geometry()
        if g and not g.isEmpty(): idx.insertFeature(f)
    kept_ids=set(); total=len(feats) or 1
    for i,f in enumerate(feats):
        g=f.geometry()
        if g is None or g.isEmpty():
            if feedback: feedback.setProgress(int(100*(i+1)/total))
            continue
        is_dup=False
        for cid in idx.intersects(g.boundingBox()):
            if cid==f.id() or cid not in kept_ids: continue
            other=lookup[cid].geometry()
            try:
                if other and g.equals(other):
                    is_dup=True; break
            except Exception:
                continue
        if is_dup:
            duplicates.append({'fid':f.id(),'duplicate_of':cid})
        else:
            kept_ids.add(f.id())
        if feedback: feedback.setProgress(int(100*(i+1)/total))
    return duplicates


def remove_duplicates(geometries,duplicate_fids):
    """Drop the given fids from a {fid: geometry} mapping."""
    return {fid:g for fid,g in geometries.items() if fid not in duplicate_fids}


# ---------------------------------------------------------------------------
# Guaranteed zero-overlap cutting (priority to the underneath feature)
# ---------------------------------------------------------------------------

def _topology_rebuild_by_priority(geometries, order=None, feedback=None):
    """Rebuild overlapping polygons from one shared, fully-noded boundary network.

    IMPORTANT: do not use ``native:union`` here.  QGIS's Union algorithm is
    not a guaranteed same-layer planar subdivision: features on the same
    input layer do not necessarily split one another.  That was the main
    reason the previous implementation could still leave a missing-vertex /
    microscopic shared-boundary problem after an apparent overlap repair.

    Instead:
      1. normalize every source polygon to the same precision grid;
      2. collect ALL polygon boundaries;
      3. unary-union the boundaries to node every crossing/intersection;
      4. polygonize that one common line network into atomic faces;
      5. assign each face to the highest-priority source which contains it;
      6. union the owned faces back per source feature.

    Because every output polygon is reconstructed from the same noded faces,
    neighbouring polygons inherit exactly the same segment and vertex
    coordinates.  This is the critical property required for QGIS topology
    checks involving shared boundaries and missing vertices.
    """
    if not geometries:
        return {}

    ordered=list(order) if order is not None else list(geometries.keys())
    priority={fid:i for i,fid in enumerate(ordered)}

    # All overlay participants must already be on the same precision model.
    working=_normalise_polygon_geometries(geometries)
    source_items=[
        (fid, working[fid]) for fid in ordered
        if working.get(fid) is not None and not working[fid].isEmpty()
    ]
    if not source_items:
        return {fid:QgsGeometry() for fid in ordered}

    try:
        # Build one common boundary network.  unaryUnion nodes the linework at
        # every intersection, including intersections which did not exist as
        # vertices in either original polygon.
        boundaries=[]
        for fid,g in source_items:
            try:
                b=g.boundary()
                if b is not None and not b.isEmpty():
                    boundaries.append(b)
            except Exception:
                continue

        if not boundaries:
            return None

        noded=QgsGeometry.unaryUnion(boundaries)
        if noded is None or noded.isEmpty():
            return None

        # QGIS documents polygonize() as requiring fully noded linework and
        # specifically recommends unaryUnion() first.  polygonize() is
        # available from QGIS 3.0, so this remains compatible with the plugin's
        # QGIS 3.16 minimum.
        faces_geom=QgsGeometry.polygonize([noded])
        if faces_geom is None or faces_geom.isEmpty():
            return None

        faces=_explode_to_singlepart_polygons(faces_geom)
        if not faces:
            return None

        # Assign each atomic face to the highest-priority source polygon which
        # actually owns the face.  pointOnSurface() is guaranteed to be on the
        # face interior for a valid polygon face, so it avoids boundary-point
        # ambiguity at shared vertices.
        assigned={fid:[] for fid in ordered}
        total=len(faces) or 1
        for i,face in enumerate(faces):
            face=_clean_polygon_result(face)
            if face is None or face.isEmpty():
                continue

            try:
                probe=face.pointOnSurface()
            except Exception:
                probe=face.centroid()
            if probe is None or probe.isEmpty():
                continue

            owners=[]
            for fid,src_g in source_items:
                try:
                    # contains() is the preferred ownership test.  The
                    # intersects() fallback handles a rare point-on-boundary
                    # numerical case without changing the priority rule.
                    if src_g.contains(probe):
                        owners.append(fid)
                    elif src_g.intersects(probe) and src_g.distance(probe) == 0:
                        owners.append(fid)
                except Exception:
                    continue

            if owners:
                winner=max(owners,key=lambda fid:priority[fid])
                assigned[winner].append(face)

            if feedback:
                feedback.setProgress(int(100*(i+1)/total))

        # Unioning faces belonging to the same owner dissolves artificial
        # internal face edges.  Shared edges between different owners remain
        # exactly the same noded linework on both sides.
        result={}
        for fid in ordered:
            pieces=assigned.get(fid,[])
            if not pieces:
                result[fid]=QgsGeometry()
                continue
            merged=_safe_union(pieces)
            if merged is None or merged.isEmpty():
                result[fid]=QgsGeometry()
                continue
            result[fid]=_clean_polygon_result(merged)

        # A final common-grid pass is intentional: union/makeValid may have
        # returned a fresh geometry object, and every output must remain on the
        # same precision model before output.
        return _normalise_polygon_geometries(result)

    except Exception:
        return None


def remove_overlaps_within_layer(geometries, feedback=None, max_passes=25):
    """Remove polygon overlaps using one common noded planar topology.

    Later features have priority.  This function deliberately does not run a
    separate topology-validation gate.  It constructs the repaired geometries
    and returns them to the caller for output creation.
    """
    order=list(geometries.keys())
    if not order:
        return {},0,0.0

    working=_normalise_polygon_geometries(geometries)
    before_area=sum(g.area() for g in working.values() if g and not g.isEmpty())

    # Preferred path: reconstruct all features from one common noded boundary
    # network. This keeps shared edges and vertices based on the same linework.
    rebuilt=_topology_rebuild_by_priority(working,order=order,feedback=feedback)
    if rebuilt is not None:
        result=_normalise_polygon_geometries(rebuilt)
        after_area=sum(g.area() for g in result.values() if g and not g.isEmpty())
        removed=max(0.0,before_area-after_area)
        changed=sum(1 for fid in order
                    if not working.get(fid,QgsGeometry()).isEmpty()
                    and (result.get(fid) is None or result[fid].isEmpty()
                         or abs(working[fid].area()-result[fid].area())>0.0))
        return result,changed,removed

    # Compatibility fallback when the common noded reconstruction cannot be
    # completed. Keep the deterministic later-feature priority rule.
    result=dict(working)
    source=dict(working)
    changed=0
    removed_area=0.0
    total=len(order) or 1

    for i,fid in enumerate(order):
        g=result.get(fid)
        if g is None or g.isEmpty():
            continue
        blockers=[source[later] for later in order[i+1:]
                  if source.get(later) is not None and not source[later].isEmpty()]
        if blockers:
            try:
                blocker_union=_safe_union(blockers)
                if blocker_union is not None and not blocker_union.isEmpty():
                    trimmed=_clean_polygon_result(g.difference(blocker_union))
                    cut=max(0.0,g.area()-(trimmed.area() if not trimmed.isEmpty() else 0.0))
                    if cut>0.0:
                        changed+=1
                        removed_area+=cut
                    result[fid]=trimmed
            except Exception:
                result[fid]=g
        if feedback is not None:
            try:
                feedback.setProgress(int(((i+1)/total)*100))
            except Exception:
                pass

    return _normalise_polygon_geometries(result),changed,removed_area

def sequential_line_side_buffers(layer,distances,side,output_names=None,segments=16,feature_ids=None):
    """Generate sequential single-sided buffer band layers for a line layer.

    Section 1 spans 0..d1 from the line. Section 2 spans d1..(d1+d2), and so
    on — each new section continues exactly where the previous one ended,
    the same principle used for the polygon inward buffers.
    side: SIDE_LEFT or SIDE_RIGHT.
    """
    side_val=SIDE_LEFT if side in ('Left','left',SIDE_LEFT) else SIDE_RIGHT
    names=list(output_names or [])
    defaults=[f'line_buffer_{"L" if side_val==SIDE_LEFT else "R"}{i}' for i in range(1,len(distances)+1)]
    names += defaults[len(names):len(distances)]
    allowed=set(feature_ids) if feature_ids is not None else None
    lines=[(f.id(),QgsGeometry(f.geometry()),list(f.attributes())) for f in layer.getFeatures() if (allowed is None or f.id() in allowed) and f.geometry() and not f.geometry().isEmpty()]
    outputs=[]; cumulative=0.0; prev_buffers={fid:None for fid,_,_ in lines}
    for section,d in enumerate(distances,1):
        cumulative+=d
        band_name=names[section-1]
        band=_make_buffer_layer(layer,band_name)
        feats=[]
        for fid,geom,attrs in lines:
            try:
                cum_buf=geom.singleSidedBuffer(cumulative,segments,side_val)
            except Exception:
                cum_buf=None
            if cum_buf is None or cum_buf.isEmpty(): continue
            prev=prev_buffers.get(fid)
            band_geom=cum_buf.difference(prev) if prev is not None and not prev.isEmpty() else cum_buf
            if band_geom and not band_geom.isEmpty():
                nf=QgsFeature(band.fields()); nf.setGeometry(band_geom); nf.setAttributes(list(attrs)+[fid,section,d]); feats.append(nf)
            prev_buffers[fid]=cum_buf
        band.dataProvider().addFeatures(feats); band.updateExtents(); outputs.append(band)
    return outputs


def combine_layers(layers,name,source_crs):
    """Merge a list of already-built (not necessarily added-to-project) memory
    layers into one combined layer, tagging each feature with its source layer
    name, without dissolving — every original feature stays independently
    selectable."""
    combined=QgsVectorLayer(f'Polygon?crs={source_crs.authid()}',name,'memory') if layers and QgsWkbTypes.geometryType(layers[0].wkbType())==QgsWkbTypes.PolygonGeometry else QgsVectorLayer(f'LineString?crs={source_crs.authid()}',name,'memory')
    fields=[layers[0].fields().at(i) for i in range(layers[0].fields().count())] if layers else []
    fields=[f for f in fields if f.name()!='source_layer']
    fields.append(QgsField('source_layer',QVariant.String))
    combined.dataProvider().addAttributes(fields); combined.updateFields(); all_feats=[]
    for l in layers:
        for f in l.getFeatures():
            nf=QgsFeature(combined.fields()); nf.setGeometry(f.geometry())
            attrs=list(f.attributes())
            attrs=attrs[:combined.fields().count()-1]
            nf.setAttributes(attrs+[l.name()]); all_feats.append(nf)
    combined.dataProvider().addFeatures(all_feats); combined.updateExtents(); return combined


# ---------------------------------------------------------------------------
# Advance Digitization: merge / dissolve / clean vertices
# ---------------------------------------------------------------------------

def copy_features_layer(layer, feature_ids=None, name=None):
    """Create an in-memory copy containing only the requested feature IDs.

    Used by the shared 'Selected features only' scope control so operations
    that accept whole layers (merge/split) can still honor the same feature
    selection without editing the user's source layer.
    """
    allowed=set(feature_ids) if feature_ids is not None else None
    gtype=QgsWkbTypes.displayString(layer.wkbType())
    out=QgsVectorLayer(f'{gtype}?crs={layer.crs().authid()}',name or f'_GeoMend_{layer.name()}','memory')
    out.dataProvider().addAttributes(list(layer.fields())); out.updateFields()
    feats=[]
    for f in layer.getFeatures():
        if allowed is not None and f.id() not in allowed:
            continue
        nf=QgsFeature(out.fields()); nf.setGeometry(QgsGeometry(f.geometry())); nf.setAttributes(f.attributes())
        feats.append(nf)
    out.dataProvider().addFeatures(feats); out.updateExtents()
    return out


def merge_vector_layers(layers,output_name,target_crs=None):
    """Merge several vector layers of the same geometry type into one layer."""
    if not layers: raise ValueError('No layers selected to merge.')
    crs=target_crs or layers[0].crs()
    try:
        import processing
        result=processing.run('native:mergevectorlayers',{'LAYERS':layers,'CRS':crs,'OUTPUT':'memory:'})['OUTPUT']
        result.setName(output_name); return result
    except Exception:
        gtype=QgsWkbTypes.displayString(layers[0].wkbType())
        out=QgsVectorLayer(f'{gtype}?crs={crs.authid()}',output_name,'memory')
        field_names=[]; fields=[]
        for l in layers:
            for i in range(l.fields().count()):
                fld=l.fields().at(i)
                if fld.name() not in field_names: field_names.append(fld.name()); fields.append(fld)
        out.dataProvider().addAttributes(fields); out.updateFields(); feats=[]
        for l in layers:
            for f in l.getFeatures():
                nf=QgsFeature(out.fields()); nf.setGeometry(f.geometry())
                attrs=[f[name] if name in f.fields().names() else None for name in field_names]
                nf.setAttributes(attrs); feats.append(nf)
        out.dataProvider().addFeatures(feats); out.updateExtents(); return out


def dissolve_layer(layer,output_name,field_name=None):
    """Dissolve a layer into a single feature, or grouped by an attribute."""
    try:
        import processing
        params={'INPUT':layer,'FIELD':[field_name] if field_name else [],'SEPARATE_DISJOINT':False,'OUTPUT':'memory:'}
        result=processing.run('native:dissolve',params)['OUTPUT']
        result.setName(output_name); return result
    except Exception:
        geoms=[f.geometry() for f in layer.getFeatures() if f.geometry() and not f.geometry().isEmpty()]
        union=_safe_union(geoms)
        out=QgsVectorLayer(f'{QgsWkbTypes.displayString(layer.wkbType())}?crs={layer.crs().authid()}',output_name,'memory')
        out.dataProvider().addAttributes([QgsField('id',QVariant.Int)]); out.updateFields()
        if union and not union.isEmpty():
            nf=QgsFeature(out.fields()); nf.setGeometry(union); nf.setAttribute('id',1)
            out.dataProvider().addFeature(nf)
        out.updateExtents(); return out


def clean_vertices(layer,output_name,tolerance=0.0):
    """Remove unwanted duplicate/redundant vertices ('dots') from every
    feature in the layer."""
    try:
        import processing
        result=processing.run('native:removeduplicatevertices',{'INPUT':layer,'TOLERANCE':tolerance,'USE_Z_VALUE':False,'OUTPUT':'memory:'})['OUTPUT']
        result.setName(output_name); return result
    except Exception:
        out=QgsVectorLayer(f'{QgsWkbTypes.displayString(layer.wkbType())}?crs={layer.crs().authid()}',output_name,'memory')
        out.dataProvider().addAttributes(list(layer.fields())); out.updateFields(); feats=[]
        for f in layer.getFeatures():
            g=f.geometry()
            if g and not g.isEmpty():
                cleaned=QgsGeometry(g)
                try: cleaned.removeDuplicateNodes(tolerance if tolerance>0 else 1e-8)
                except Exception: pass
                nf=QgsFeature(out.fields()); nf.setGeometry(cleaned); nf.setAttributes(f.attributes()); feats.append(nf)
        out.dataProvider().addFeatures(feats); out.updateExtents(); return out


def explode_noncontiguous_parts(layer,output_name):
    """Split each feature into independent single-part features, but only for
    parts that are genuinely separate ('not a whole'). Parts of a multipart
    geometry that touch/overlap each other are first merged back into one
    whole shape (since together they form a single field); only the pieces
    that remain fully disjoint after that merge are cut apart into their own
    independent features.
    """
    out=QgsVectorLayer(f'{QgsWkbTypes.geometryDisplayString(QgsWkbTypes.geometryType(layer.wkbType()))}?crs={layer.crs().authid()}',output_name,'memory')
    fields=list(layer.fields())
    fields=list(fields)+[QgsField('source_fid',QVariant.Int),QgsField('part_index',QVariant.Int)]
    out.dataProvider().addAttributes(fields); out.updateFields(); feats=[]
    for f in layer.getFeatures():
        g=f.geometry()
        if g is None or g.isEmpty(): continue
        if not g.isMultipart():
            nf=QgsFeature(out.fields()); nf.setGeometry(g); nf.setAttributes(list(f.attributes())+[f.id(),1]); feats.append(nf); continue
        parts=g.asGeometryCollection()
        if len(parts)<=1:
            nf=QgsFeature(out.fields()); nf.setGeometry(g); nf.setAttributes(list(f.attributes())+[f.id(),1]); feats.append(nf); continue
        try:
            unioned=QgsGeometry.unaryUnion(parts)
            merged_parts=unioned.asGeometryCollection() if unioned.isMultipart() else [unioned]
        except Exception:
            merged_parts=parts
        # If the union produced fewer shapes, some parts were touching/overlapping
        # and are treated as one whole; otherwise every part was already disjoint.
        final_parts=merged_parts if len(merged_parts)<len(parts) else parts
        for idx,part in enumerate(final_parts,1):
            if part is None or part.isEmpty(): continue
            nf=QgsFeature(out.fields()); nf.setGeometry(part); nf.setAttributes(list(f.attributes())+[f.id(),idx]); feats.append(nf)
    out.dataProvider().addFeatures(feats); out.updateExtents(); return out


# ---------------------------------------------------------------------------
# Output Destination: save-to-disk support
# ---------------------------------------------------------------------------

_INVALID_FILENAME_CHARS='\\/:*?"<>|'

def save_layer_to_disk(layer,folder,driver='GPKG'):
    """Write an in-memory output layer to its own file on disk and return the
    layer freshly reloaded from that file, so it behaves exactly like any
    other saved layer instead of a temporary/scratch one.

    Each output layer becomes its own file (GeoPackage .gpkg or Esri
    Shapefile .shp, per `driver`), named after the layer, inside `folder`.
    Re-running the same operation with the same output name/folder overwrites
    the earlier file, mirroring the existing behaviour of replacing a
    previous temporary layer of the same name.
    """
    if not folder:
        raise ValueError('No output folder is set. Choose a folder under Output Destination to save permanently, or leave it empty to keep new layers temporary.')
    if not os.path.isdir(folder):
        raise ValueError(f'The output folder does not exist or is not accessible:\n{folder}')

    ext={'GPKG':'gpkg','ESRI Shapefile':'shp'}.get(driver,'gpkg')
    safe_name=''.join(c for c in layer.name() if c not in _INVALID_FILENAME_CHARS).strip() or 'GeoMend_output'
    path=os.path.join(folder,f'{safe_name}.{ext}')

    options=QgsVectorFileWriter.SaveVectorOptions()
    options.driverName=driver
    options.fileEncoding='UTF-8'
    options.actionOnExistingFile=QgsVectorFileWriter.CreateOrOverwriteFile

    try:
        result=QgsVectorFileWriter.writeAsVectorFormatV3(
            layer,path,QgsProject.instance().transformContext(),options)
    except AttributeError:
        # Older QGIS API fallback (pre-3.10 style two-value return).
        result=QgsVectorFileWriter.writeAsVectorFormatV2(
            layer,path,QgsProject.instance().transformContext(),options)

    error_code=result[0] if isinstance(result,tuple) else result
    if error_code!=QgsVectorFileWriter.NoError:
        detail=result[1] if isinstance(result,tuple) and len(result)>1 else str(error_code)
        raise RuntimeError(f'Could not save layer "{layer.name()}" to disk:\n{path}\n\n{detail}')

    loaded=QgsVectorLayer(path,layer.name(),'ogr')
    if not loaded.isValid():
        raise RuntimeError(f'Saved "{layer.name()}" to:\n{path}\n\nbut QGIS could not reopen it from disk.')
    return loaded
