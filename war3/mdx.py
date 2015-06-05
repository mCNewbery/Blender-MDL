"""This module contains an importer for the MDX format."""


import io
import struct
from .model import *

__all__ = ["LoadError", "Loader", "load"]


def partition(elements, counts):
    i = 0
    for n in counts:
        li = []
        for _ in range(n):
            li.append(elements[i])
            i += 1
        yield li


class LoadError(Exception):
    pass


class _ReadonlyBytesIO:
    def __init__(self, buf, idx=0):
        self.buf = buf
        self.idx = idx

    def read(self, n=-1):
        idx = self.idx
        if n < 0 or len(self.buf) - idx < n:
            self.idx = len(self.buf)
            return self.buf[self.idx:]
        else:
            self.idx += n
            return self.buf[idx:self.idx]


def _scalar_or_tuple(li):
    return li[0] if len(li) == 1 else tuple(li)


class _BaseLoader:
    """Contains utility methods that make writing the Loader easier."""

    def __init__(self, infile):
        self.infile = infile
        self.infile_stack = []

    def push_infile(self, infile):
        self.infile_stack.append(self.infile)
        self.infile = infile

    def pop_infile(self):
        infile = self.infile
        self.infile = self.infile_stack.pop()
        return infile

    def check_block_magic(self, magic):
        buf = self.infile.read(4)
        if buf != magic:
            raise LoadError("expected %s, not %s" % (magic, buf))

    def load_block(self):
        n, = struct.unpack('<i', self.infile.read(4))
        if n < 0:
            raise LoadError("expected a positive integer")
        return self.infile.read(n)

    def load_multiblocks(self, magic, loader_fn, optional=False):
        if optional:
            if magic != self.infile.read(4):
                self.infile.seek(-4, io.SEEK_CUR)
                return
        else:
            self.check_block_magic(magic)
        buf = self.load_block()

        i, n = 0, len(buf)
        while i < n:
            m, = struct.unpack_from('<i', buf, i)
            self.push_infile(_ReadonlyBytesIO(buf, i + 4))
            loader_fn(m - 4)
            self.pop_infile()
            i += m

    def load_vectors(self, magic, type_='<3f'):
        self.check_block_magic(magic)
        n, = struct.unpack('<i', self.infile.read(4))
        m = struct.calcsize(type_)
        vectors = []

        for _ in range(n):
            t = struct.unpack(type_, self.infile.read(m))
            vectors.append(t[0] if len(t) == 1 else t)

        return vectors

    def load_keyframe(self, target, type_):
        nkeys, ltype, gsid = struct.unpack('<3i', self.infile.read(12))
        ltype = LineType(ltype)
        n = 16 # 4B for block magic

        anim = KeyframeAnimation(target, ltype, gsid)
        parse_val, parse_tan = [s.format(t=type_)
                                for s in ('<i {t}', '<{t} {t}')]
        sz_val, sz_tan = [struct.calcsize(s) for s in (parse_val, parse_tan)]

        for _ in range(nkeys):
            frame, *value = struct.unpack(parse_val, self.infile.read(sz_val))
            value = _scalar_or_tuple(value)
            n += sz_val

            if ltype in (LineType.Hermite, LineType.Bezier):
                tangents = struct.unpack(parse_tan, self.infile.read(sz_tan))
                ntan = len(tangents) // 2
                tan_in, tan_out = tangents[:ntan], tangents[ntan:]
                tan_in = _scalar_or_tuple(tan_in)
                tan_out = _scalar_or_tuple(tan_out)
                n += sz_tan
            else:
                tan_in = tan_out = None

            anim.keyframes.append(Keyframe(frame, value, tan_in, tan_out))

        return n, anim

    def load_object(self, flag_class=ObjectFlag):
        k, = struct.unpack('<i', self.infile.read(4))
        name = self.infile.read(80).rstrip(b'\x00').decode('ascii')
        obj_id, = struct.unpack('<i', self.infile.read(4))
        parent, = struct.unpack('<i', self.infile.read(4))
        flags, = struct.unpack('<i', self.infile.read(4))
        flags = flag_class.set_from_int(flags)

        j, anims = 96, []
        while j < k:
            m, anim = self.load_object_keyframe()
            anims.append(anim)
            j += m

        return k, {'name': name, 'object_id': obj_id, 'parent': parent,
                   'flags': flags, 'animations': anims}

    def load_object_keyframe(self):
        magic = self.infile.read(4)
        if magic == b'KGTR':
            target = KF.ObjectTranslation
            type_ = '3f'
        elif magic == b'KGRT':
            target = KF.ObjectRotation
            type_ = '4f'
        elif magic == b'KGSC':
            target = KF.ObjectScaling
            type_ = '3f'
        elif magic == b'KATV':
            target = KF.ObjectVisibility
            type_ = 'f'
        else:
            raise LoadError("expected KG{TR,RT,SC} or KATV, not %s"
                            % magic.decode('ascii'))

        return self.load_keyframe(target, type_)


class Loader(_BaseLoader):
    def __init__(self, infile):
        _BaseLoader.__init__(self, infile)
        self.model = Model()

    def load(self):
        self.check_magic_number()
        self.load_version()
        self.load_modelinfo()
        self.load_sequences()
        self.load_global_sequences()
        self.load_materials()
        self.load_textures()
        # XXX: load_texture_animation is untested
        self.load_texture_animations()
        self.load_geosets()
        self.load_geoset_animations()
        self.load_bones()
        self.load_lights()
        self.load_helpers()
        self.load_attachements()
        self.load_pivot_points()
        # XXX: load_particle_emitters is untested
        self.load_particle_emitters()
        self.load_particle_emitters_2()
        # TODO: load RIBB (ribbon emitter) blocks
        # TODO: load EVTS (events) blocks
        # TODO: load CLID (collision shape) blocks
        return self.model

    def check_magic_number(self):
        if self.infile.read(4) != b'MDLX':
            raise LoadError("not a MDX file")

    def load_version(self):
        self.check_block_magic(b'VERS')
        buf = self.load_block()
        self.model.version, = struct.unpack('<i', buf)

    def load_modelinfo(self):
        self.check_block_magic(b'MODL')
        buf = self.load_block()

        name, = struct.unpack_from('<80s', buf)
        name = name.rstrip(b'\x00').decode("ascii")
        bounds_radius, = struct.unpack_from('<f', buf, 80)
        min_extent = struct.unpack_from('<3f', buf, 84)
        max_extent = struct.unpack_from('<3f', buf, 96)
        blend_time, = struct.unpack_from('<i', buf, 108)

        self.model.model = ModelInfo(name, bounds_radius,
                                     min_extent, max_extent, blend_time)

    def load_sequences(self):
        self.check_block_magic(b'SEQS')
        buf = self.load_block()
        fmt = '<80s 2i f i f 4x f 3f 3f'

        for i in range(0, len(buf), struct.calcsize(fmt)):
            t = struct.unpack_from(fmt, buf, i)

            name = t[0].rstrip(b'\x00').decode("ascii")
            interval = t[1:3]
            move_speed = t[4]
            non_looping = bool(t[5])
            rarity = t[6]
            bounds_radius = t[7]
            min_extent = t[8:11]
            max_extent = t[11:]

            self.model.sequences.append(
                Animation(name, interval, move_speed, non_looping, rarity,
                          bounds_radius, min_extent, max_extent)
            )

    def load_global_sequences(self):
        magic = self.infile.read(4)
        if magic != b'GLBS':
            self.infile.seek(-4, io.SEEK_CUR)
            return

        buf = self.load_block()
        i, n = 0, len(buf)
        while i < n:
            duration, = struct.unpack_from('<i', buf, i)
            self.model.global_sequences.append(duration)
            i += 4

    def load_materials(self):
        self.check_block_magic(b'MTLS')
        buf = self.load_block()
        i, n = 0, len(buf)

        while i < n:
            t = struct.unpack_from('<i i i', buf, i)
            mat = Material(t[1], bool(t[2] & 0x01),
                           bool(t[2] & 0x10), bool(t[2] & 0x20))

            # HACK: let load_layers() read from existing data
            self.push_infile(_ReadonlyBytesIO(buf, i + 12))
            mat.layers = self.load_layers()
            self.pop_infile()

            self.model.materials.append(mat)
            i += t[0]

    def load_layers(self):
        self.check_block_magic(b'LAYS')
        nlays, = struct.unpack('<i', self.infile.read(4))
        fmt = '<5i f'
        lays = []

        for _ in range(nlays):
            n, = struct.unpack('<i', self.infile.read(4))
            buf = self.infile.read(n - 4)

            t = struct.unpack_from(fmt, buf)
            layer = Layer(t[0], bool(t[1] & 0x01), bool(t[1] & 0x02),
                          bool(t[1] & 0x10), bool(t[1] & 0x20),
                          bool(t[1] & 0x40), bool(t[1] & 0x80),
                          t[2], t[3], t[4], t[5])

            j, n = struct.calcsize(fmt), len(buf)
            while j < n:
                self.push_infile(_ReadonlyBytesIO(buf, j))
                m, anim = self.load_material_keyframe()
                self.pop_infile()
                layer.animations.append(anim)
                j += m

            lays.append(layer)

        return lays

    def load_material_keyframe(self):
        magic = self.infile.read(4)
        if magic == b'KMTA':
            target = KF.MaterialAlpha
            type_ = 'f'
        elif magic == b'KMTF':
            target = KF.MaterialTexture
            type_ = 'i'
        else:
            raise LoadError("exptected KMT{A,F}, not %s"
                            % magic.decode("ascii"))

        return self.load_keyframe(target, type_)

    def load_textures(self):
        self.check_block_magic(b'TEXS')
        buf = self.load_block()
        fmt = '<i 256s 4x i'

        for i in range(0, len(buf), struct.calcsize(fmt)):
            t = struct.unpack_from(fmt, buf, i)
            rid = t[0]
            path = t[1].rstrip(b'\x00').decode("ascii")
            wrap_w = bool(t[2] & 1)
            wrap_h = bool(t[2] & 2)
            self.model.textures.append(Texture(rid, path, wrap_w, wrap_h))

    def load_texture_animations(self):
        self.load_multiblocks(b'TXAN', self.load_texture_animation_keyframes,
                              optional=True)

    def load_texture_animation_keyframes(self, k):
        j, anims = 0, []
        while j < k:
            m, anim = self.load_texture_animation_keyframe()
            j += m
            anims.append(anim)
        self.model.texture_animations.append(anims)

    def load_texture_animation_keyframe(self):
        magic = self.infile.read(4)
        if magic == b'KTAT':
            target = KF.TextureAnimTranslation
        elif magic == b'KTAR':
            target = KF.TextureAnimRotation
        elif magic == b'KTAS':
            target = KF.TextureAnimScaling
        else:
            raise LoadError("exptected KTA{T,R,S}, not %s"
                            % magic.decode("ascii"))

        return self.load_keyframe(target, '3f')

    def load_geosets(self):
        self.load_multiblocks(b'GEOS', self.load_geoset)

    def load_geoset(self, _):
        verts = self.load_vectors(b'VRTX')
        norms = self.load_vectors(b'NRMS')
        faces = self.load_faces()
        vgrps = self.load_vectors(b'GNDX', '<B')
        groups = self.load_groups()
        attrs = self.load_geoset_attributes()
        danim, anims = self.load_ganimations()
        tverts = self.load_tvertices()

        self.model.geosets.append(Geoset(verts, norms, faces, vgrps, groups,
                                         attrs, danim, anims, tverts))

    def load_faces(self):
        ptyps = [PrimitiveType(t)
                 for t in self.load_vectors(b'PTYP', '<i')]

        pcnts = self.load_vectors(b'PCNT', '<i')
        assert len(ptyps) == len(pcnts)

        pvtx = self.load_vectors(b'PVTX', '<h')
        assert len(pvtx) == sum(pcnts)

        return [Primitives(*t) for t in zip(ptyps, partition(pvtx, pcnts))]

    def load_groups(self):
        mtgcs = self.load_vectors(b'MTGC', '<i')
        mats = self.load_vectors(b'MATS', '<i')
        return list(partition(mats, mtgcs))

    def load_geoset_attributes(self):
        material_id, = struct.unpack('<i', self.infile.read(4))
        selection_grp, = struct.unpack('<i', self.infile.read(4))
        selectable, = struct.unpack('<i', self.infile.read(4))
        return GeosetAttributes(material_id, selection_grp, selectable != 4)

    def load_ganimations(self):
        bounds_radius, = struct.unpack('<f', self.infile.read(4))
        min_ext = struct.unpack('<3f', self.infile.read(12))
        max_ext = struct.unpack('<3f', self.infile.read(12))
        def_anim = GAnimation(bounds_radius, min_ext, max_ext)

        n, = struct.unpack('<i', self.infile.read(4))
        anims = []
        for _ in range(n):
            bounds_radius, = struct.unpack('<f', self.infile.read(4))
            min_ext = struct.unpack('<3f', self.infile.read(12))
            max_ext = struct.unpack('<3f', self.infile.read(12))
            anims.append(GAnimation(bounds_radius, min_ext, max_ext))

        return def_anim, anims

    def load_tvertices(self):
        self.check_block_magic(b'UVAS')
        n, = struct.unpack('<i', self.infile.read(4))
        tverts = []

        for _ in range(n):
            tverts.append(self.load_vectors(b'UVBS', '<2f'))

        return tverts

    def load_geoset_animations(self):
        self.load_multiblocks(b'GEOA', self.load_geoset_animation)

    def load_geoset_animation(self, max_bytes):
        alpha, = struct.unpack('<f', self.infile.read(4))
        color_anim, = struct.unpack('<i', self.infile.read(4))
        color_anim = ColorAnimation(color_anim)
        color = struct.unpack('<3f', self.infile.read(12))
        geoset_id, = struct.unpack('<i', self.infile.read(4))

        frames = []
        i = 24
        while i < max_bytes:
            j, frame = self.load_geoset_animation_keyframe()
            frames.append(frame)
            i = i + j

        anim = GeosetAnimation(alpha, color_anim, color, geoset_id, frames)
        self.model.geoset_animations.append(anim)

    def load_geoset_animation_keyframe(self):
        magic = self.infile.read(4)
        if magic == b'KGAO':
            target = KF.GeosetAnimAlpha
            type_ = 'f'
        elif magic == b'KGAC':
            target = KF.GeosetAnimColor
            type_ = '3f'
        else:
            raise LoadError("expected KGA{O,C}, not %s"
                            % magic.decode('ascii'))

        return self.load_keyframe(target, type_)

    def load_bones(self):
        self.check_block_magic(b'BONE')
        buf = self.load_block()

        i, n = 0, len(buf)
        while i < n:
            self.push_infile(_ReadonlyBytesIO(buf, i))
            m, obj = self.load_object()
            obj['geoset_id'], = struct.unpack('<i', self.infile.read(4))
            obj['geoset_anim_id'], = struct.unpack('<i', self.infile.read(4))
            self.pop_infile()
            self.model.bones.append(Bone(**obj))
            i += m + 8

    def load_lights(self):
        self.load_multiblocks(b'LITE', self.load_light, optional=True)

    def load_light(self, max_bytes):
        j, obj = self.load_object()
        obj['type_'], = struct.unpack('<i', self.infile.read(4))
        obj['type_'] = LightType(obj['type_'])
        obj['attenuation'] = struct.unpack('<2f', self.infile.read(8))
        obj['color'] = struct.unpack('<3f', self.infile.read(12))
        obj['intensity'], = struct.unpack('<f', self.infile.read(4))
        obj['ambient_color'] = struct.unpack('<3f', self.infile.read(12))
        obj['ambient_intensity'], = struct.unpack('<f', self.infile.read(4))
        j += 44

        anims = []
        while j < max_bytes:
            m, anim = self.load_light_keyframe()
            anims.append(anim)
            j += m

        obj['animations'].extend(anims)
        self.model.lights.append(Light(**obj))

    def load_light_keyframe(self):
        magic = self.infile.read(4)
        if magic == b'KLAV':
            target = KF.LightVisibility
            type_ = 'f'
        elif magic == b'KLAC':
            target = KF.LightColor
            type_ = '3f'
        elif magic == b'KLAI':
            target = KF.LightIntensity
            type_ = 'f'
        elif magic == b'KLBC':
            target = KF.LightAmbientColor
            type_ = '3f'
        elif magic == b'KLBI':
            target = KF.LightAmbientIntensity
            type_ = 'f'
        else:
            raise LoadError('expected KL{AV,AC,AI,BC,BI}, not %s'
                            % magic.decode('ascii'))

        return self.load_keyframe(target, type_)

    def load_helpers(self):
        magic = self.infile.read(4)
        if magic != b'HELP':
            self.infile.seek(-4, io.SEEK_CUR)
            return
        buf = self.load_block()

        i, n = 0, len(buf)
        while i < n:
            self.push_infile(_ReadonlyBytesIO(buf, i))
            m, h = self.load_helper()
            self.pop_infile()
            self.model.helpers.append(h)
            i += m

    def load_helper(self):
        j, obj = self.load_object()
        return j, Helper(**obj)

    def load_attachements(self):
        self.load_multiblocks(b'ATCH', self.load_attachement, optional=True)

    def load_attachement(self, max_bytes):
        j, obj = self.load_object()
        obj['path'] = self.infile.read(256).rstrip(b'\x00').decode('ascii')
        # XXX: is this really just padding or does it do anything
        self.infile.read(4)
        obj['attachement_id'], = struct.unpack('<i', self.infile.read(4))
        j += 264

        while j < max_bytes:
            m, anim = self.load_attachement_keyframe()
            obj['animations'].append(anim)
            j += m

        self.model.attachements.append(Attachement(**obj))

    def load_attachement_keyframe(self):
        magic = self.infile.read(4)
        if magic != b'KATV':
            raise LoadError("expected KATV, not %s"
                            % magic.decode('ascii'))

        return self.load_keyframe(KF.AttachementVisibility, 'f')

    def load_pivot_points(self):
        self.check_block_magic(b'PIVT')
        n, = struct.unpack('<i', self.infile.read(4))
        for _ in range(n // 12):
            self.model.pivot_points.append(struct.unpack('<3f', self.infile.read(12)))

    def load_particle_emitters(self):
        self.load_multiblocks(b'PREM', self.load_particle_emitter, optional=True)

    def load_particle_emitter(self, max_bytes):
        j, obj = self.load_object(flag_class=ParticleFlag)
        obj['emission_rate'], = struct.unpack('<f', self.infile.read(4))
        obj['gravity'], = struct.unpack('<f', self.infile.read(4))
        obj['longitude'], = struct.unpack('<f', self.infile.read(4))
        obj['latitude'], = struct.unpack('<f', self.infile.read(4))
        obj['model_path'] = self.infile.read(256).rstrip(b'\x00').decode('ascii')
        # XXX: is this really just padding?
        obj['life_span'], = struct.unpack('<f', self.infile.read(4))
        obj['init_velocity'], = struct.unpack('<f', self.infile.read(4))
        j += 280

        while j < max_bytes:
            m, anim = self.load_particle_emitter_keyframe()
            obj['animations'].append(anim)
            j += m

        self.model.particle_emitters.append(ParticleEmitter(**obj))

    def load_particle_emitter_keyframe(self):
        magic = self.infile.read(4)
        if magic != b'KPEV':
            raise LoadError("expected KPEV, not %s"
                            % magic.decode('ascii'))

        return self.load_keyframe(KF.ParticleEmitterVisibility, 'f')

    def load_particle_emitters_2(self):
        self.load_multiblocks(b'PRE2', self.load_particle_emitter_2, optional=True)

    def load_particle_emitter_2(self, max_bytes):
        j, obj = self.load_object(flag_class=ParticleFlag2)
        obj['speed'], = struct.unpack('<f', self.infile.read(4))
        obj['variation'], = struct.unpack('<f', self.infile.read(4))
        obj['latitude'], = struct.unpack('<f', self.infile.read(4))
        obj['gravity'], = struct.unpack('<f', self.infile.read(4))
        obj['lifespan'], = struct.unpack('<f', self.infile.read(4))
        obj['emission_rate'], = struct.unpack('<f', self.infile.read(4))
        obj['length'], = struct.unpack('<f', self.infile.read(4))
        obj['width'], = struct.unpack('<f', self.infile.read(4))
        obj['filter_mode'] = FilterMode(struct.unpack('<i', self.infile.read(4))[0])
        obj['rows'], = struct.unpack('<i', self.infile.read(4))
        obj['columns'], = struct.unpack('<i', self.infile.read(4))
        obj['tail_mode'] = TailMode(struct.unpack('<i', self.infile.read(4))[0])
        obj['tail_length'], = struct.unpack('<f', self.infile.read(4))
        obj['time'], = struct.unpack('<f', self.infile.read(4))
        obj['segment_color'] = [struct.unpack('<3f', self.infile.read(12))
                                for _ in range(3)]
        obj['alpha'] = struct.unpack('<3B', self.infile.read(3))
        obj['particle_scaling'] = struct.unpack('<3f', self.infile.read(12))
        obj['lifespan_uv_anim'] = struct.unpack('<3i', self.infile.read(12))
        obj['decay_uv_anim'] = struct.unpack('<3i', self.infile.read(12))
        obj['tail_uv_anim'] = struct.unpack('<3i', self.infile.read(12))
        obj['tail_decay_uv_anim'] = struct.unpack('<3i', self.infile.read(12))
        obj['texture_id'], = struct.unpack('<i', self.infile.read(4))
        obj['squirt'] = bool(struct.unpack('<i', self.infile.read(4))[0])
        obj['priority_plane'], = struct.unpack('<i', self.infile.read(4))
        obj['replaceable_id'], = struct.unpack('<i', self.infile.read(4))
        j += 171

        while j < max_bytes:
            m, anim = self.load_particle_emitter_keyframe_2()
            obj['animations'].append(anim)
            j += m

        self.model.particle_emitters_2.append(ParticleEmitter2(**obj))

    def load_particle_emitter_keyframe_2(self):
        magic = self.infile.read(4)
        if magic == b'KP2S':
            target = KF.ParticleEmitter2Speed
        elif magic == b'KP2L':
            target = KF.ParticleEmitter2Latitude
        elif magic == b'KP2E':
            target = KF.ParticleEmitter2EmissionRate
        elif magic == b'KP2V':
            target = KF.ParticleEmitter2Visibility
        elif magic == b'KP2N':
            target = KF.ParticleEmitter2Length
        elif magic == b'KP2W':
            target = KF.ParticleEmitter2Width
        else:
            raise LoadError("expected KP2[SLEVNW], not %s"
                            % magic.decode('ascii'))

        return self.load_keyframe(target, 'f')


def load(infile):
    if isinstance(infile, str):
        infile = open(infile, 'rb')
    return Loader(infile).load()
