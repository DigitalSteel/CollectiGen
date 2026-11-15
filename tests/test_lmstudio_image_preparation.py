"""Generated in-memory/on-disk images only; no library or model fixtures."""
from dataclasses import FrozenInstanceError
import hashlib
import io
import threading

from PIL import Image, ImageFile, PngImagePlugin
import pytest

from core import lmstudio_image_preparation as prep
from core import image_derivatives


def image_file(tmp_path, *, mode='RGB', format='PNG', orientation=None):
    path = tmp_path/('generated.'+format.lower())
    color = {'RGB':(10,20,30),'RGBA':(255,0,0,0),'L':20,'LA':(20,0),'P':1}[mode]
    image = Image.new(mode,(2,3),color)
    options = {}
    if orientation is not None:
        exif = Image.Exif();exif[274]=orientation;exif[305]='generated-private-software'
        options['exif'] = exif
    if format=='PNG':
        text = PngImagePlugin.PngInfo();text.add_text('parameters','generated-private-prompt')
        options['pnginfo'] = text
        options['icc_profile'] = b'generated-profile-metadata'
    try:
        image.save(path,format=format,**options)
    finally:
        image.close()
    return path


@pytest.mark.parametrize('mode',['RGB','RGBA','L','LA'])
def test_metadata_free_representation_preserves_original_and_family(tmp_path,monkeypatch,mode):
    path = image_file(tmp_path,mode=mode)
    family = [path,path.with_suffix('.tags.json'),path.with_suffix('.txt'),path.with_suffix('.xmp')]
    for member in family[1:]:member.write_bytes(b'generated-private-family')
    before = {p:p.read_bytes() for p in family}
    calls = []
    original = prep.read_stable_training_source
    def read(source,**kwargs):
        calls.append(source)
        return original(source,**kwargs)
    monkeypatch.setattr(prep,'read_stable_training_source',read)
    maximum, truncated = Image.MAX_IMAGE_PIXELS,ImageFile.LOAD_TRUNCATED_IMAGES
    prepared = prep.prepare_image(path)
    assert calls == [path]
    assert {p:p.read_bytes() for p in family} == before
    assert set(tmp_path.iterdir()) == set(family)
    assert prepared.source_sha256 == hashlib.sha256(before[path]).hexdigest()
    assert prepared.payload_sha256 == hashlib.sha256(prepared.encoded).hexdigest()
    assert prepared.mime=='image/png' and prepared.display_size==(2,3)
    with Image.open(io.BytesIO(prepared.encoded)) as output:
        assert output.mode=='RGB' and output.info=={} and len(output.getexif())==0
        if mode in {'RGBA','LA'}:assert output.getpixel((0,0))==(255,255,255)
    assert b'generated-private' not in prepared.encoded
    assert 'generated' not in repr(prepared) and 'path' not in prepared.audit()
    assert prepared.audit()['metadata_copied'] is False
    assert (Image.MAX_IMAGE_PIXELS,ImageFile.LOAD_TRUNCATED_IMAGES)==(maximum,truncated)
    with pytest.raises(FrozenInstanceError):prepared.encoded=b'changed'


def test_orientation_is_applied_without_original_metadata_or_resize(tmp_path):
    path = image_file(tmp_path,orientation=6)
    before = path.read_bytes()
    prepared = prep.prepare_image(path)
    assert prepared.source_size==(2,3) and prepared.display_size==(3,2)
    assert prepared.orientation==6 and path.read_bytes()==before
    with Image.open(io.BytesIO(prepared.encoded)) as output:
        assert output.size==(3,2) and output.info=={}


@pytest.mark.parametrize('format',['JPEG','WEBP'])
def test_other_shared_decoder_formats(tmp_path,format):
    path = image_file(tmp_path,format=format)
    prepared = prep.prepare_image(path)
    assert prepared.source_format==format and prepared.mime=='image/png'


@pytest.mark.parametrize('kind',['palette','animated','malformed'])
def test_unsupported_inputs_are_content_free(tmp_path,kind):
    path = tmp_path/'generated-private-source.png'
    if kind=='palette':path=image_file(tmp_path,mode='P')
    elif kind=='malformed':path.write_bytes(b'generated-private-not-an-image')
    else:
        first=Image.new('RGB',(2,2),'red');second=Image.new('RGB',(2,2),'blue')
        try:first.save(path,format='GIF',save_all=True,append_images=[second])
        finally:first.close();second.close()
    with pytest.raises(prep.ImagePreparationError) as error:prep.prepare_image(path)
    assert str(error.value)=='image_preparation_failed' and error.value.__context__ is None


def test_source_payload_and_pixel_budgets(tmp_path,monkeypatch):
    path=image_file(tmp_path)
    with monkeypatch.context() as guard:
        guard.setattr(prep,'MAX_SOURCE_BYTES',path.stat().st_size-1)
        with pytest.raises(prep.ImagePreparationError,match='^source_byte_limit$'):prep.prepare_image(path)
    with monkeypatch.context() as guard:
        guard.setattr(prep,'MAX_IMAGE_BYTES',16)
        with pytest.raises(prep.ImagePreparationError,match='^prepared_image_too_large$'):prep.prepare_image(path)
    with monkeypatch.context() as guard:
        guard.setattr(image_derivatives,'MAX_PIXELS',4)
        with pytest.raises(prep.ImagePreparationError,match='^image_preparation_failed$'):prep.prepare_image(path)


def test_canonical_redirect_stale_refusal_is_not_echoed(tmp_path,monkeypatch):
    path=image_file(tmp_path)
    def refused(*args,**kwargs):raise RuntimeError('generated-private-path')
    monkeypatch.setattr(prep,'read_stable_training_source',refused)
    with pytest.raises(prep.ImagePreparationError) as error:prep.prepare_image(path)
    assert str(error.value)=='source_identity_unavailable' and error.value.__context__ is None


@pytest.mark.parametrize('phase',['before-read','after-read','after-decode','after-encode'])
def test_cancellation_closes_images_and_buffers(tmp_path,monkeypatch,phase):
    path=image_file(tmp_path);cancel=threading.Event();images=[];buffers=[]
    if phase=='before-read':cancel.set()
    original_read=prep._read_source;original_decode=prep._decode;original_buffer=prep._BoundedPNG
    def read(*args):
        value=original_read(*args)
        if phase=='after-read':cancel.set()
        return value
    def decode(raw):
        image=original_decode(raw);images.append(image)
        if phase=='after-decode':cancel.set()
        return image
    class Buffer(original_buffer):
        def __init__(self):super().__init__();buffers.append(self)
        def getvalue(self):
            value=super().getvalue()
            if phase=='after-encode':cancel.set()
            return value
    monkeypatch.setattr(prep,'_read_source',read);monkeypatch.setattr(prep,'_decode',decode)
    monkeypatch.setattr(prep,'_BoundedPNG',Buffer)
    with pytest.raises(InterruptedError):prep.prepare_image(path,should_cancel=cancel.is_set)
    assert all(buffer.closed for buffer in buffers)
    for image in images:
        with pytest.raises(ValueError):image.getpixel((0,0))


def test_source_mutation_after_preparation_is_refused(tmp_path):
    path=image_file(tmp_path);prepared=prep.prepare_image(path)
    path.write_bytes(path.read_bytes()+b'generated-external-change')
    with pytest.raises(prep.ImagePreparationError,match='^source_changed$'):
        prep.verify_source_current(path,prepared)
