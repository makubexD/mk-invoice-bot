"""Pure reading and checking of invoice data: QR payloads, images and XML (spec 03 §5).

Imports nothing from the pipeline, queue, store or channels. The OpenCV and zxing modules
(``image``, ``qr``) run only in the helper process started by the extract handler (D78).
"""
