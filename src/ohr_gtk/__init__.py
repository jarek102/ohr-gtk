"""A GTK4/libadwaita front end for :mod:`ohr`.

Separate from the library on purpose. ``ohr`` holds the specification, the vectors and
the codecs, and has no dependencies; everything here is one platform's opinion about
how to show them. The dependency points one way only.

What this is today: a surface for **manual testing**, built so a newly specified
command becomes a button by adding a row to :mod:`ohr_gtk.catalogue`. It is also the
skeleton of the application the roadmap calls M5 and M6, which is why the status page
is written as though someone will leave it open rather than glance at it.
"""

__version__ = "0.0.1"
