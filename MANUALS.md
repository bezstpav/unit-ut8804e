# Manufacturer manuals

Links checked on 2026-11-08. These are vendor documents, not part of this
repository's MIT-licensed implementation. Download them from the publisher;
no manual PDF is redistributed here.

- [UNI-T UT8804E product page and downloads](https://uni-trendus.com/products/ut8804e)
- [UT8804E user manual, HQ PDF](https://storage.googleapis.com/uni-tdocuments/Library/UT8804E%20Digital%20Multimeters%20-%20User%20Manual_HQ.pdf)
- [UT8804E user manual, alternate PDF](https://storage.googleapis.com/uni-tdocuments/Library/UT8804E%20User%20Manual.pdf)

## Clock setting

The user manual's “Time and Date” section (printed page 39, PDF page 40 in
the reviewed edition) describes front-panel setting: open Settings, choose
Time and Date / Setup, select a position with the arrow buttons, change its
value with F2/F3 and confirm with F1; F4 exits.

This Python package currently has no clock-read or clock-set API. That is a
limit of the implemented command set, not proof that every firmware lacks a
remote clock command. Do not report a USB clock change as successful without
an established command and independent confirmation.

## Programming-manual applicability

The product page also links a document labelled
[UT800E / UT8804E / UT8803E / UT8802E programming manual](https://storage.googleapis.com/uni-tdocuments/Library/UT800E%20Series%20%26%20UT8804E%2CUT8803E%2CUT8802E%20Digital%20Multimeters%20-%20Programming%20Manual.pdf).
The retrieved document identifies itself as the UT8800E programming manual;
its supported-model table omits UT8804E and describes UCI-level calls for other
models. Do not treat its filename or product-page label as validation of the
UT8804E binary USB command set. This library's implemented packet contract is
in [PROTOCOL.md](PROTOCOL.md).
