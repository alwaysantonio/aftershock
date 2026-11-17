"""
List filters for /api/earthquakes/. Every value is validated before it reaches
the database; invalid input returns 400 in the standard error shape.
"""

from django import forms
from django_filters import rest_framework as filters

from .models import Earthquake, EventStatus


class BBoxField(forms.CharField):
    """`bbox(geographic bounding box)=min_lon,min_lat,max_lon,max_lat` → a validated 4-tuple of floats."""

    def to_python(self, value):
        value = super().to_python(value)
        if not value:
            return None
        parts = value.split(",")
        if len(parts) != 4:
            raise forms.ValidationError("bbox must be min_lon,min_lat,max_lon,max_lat")
        try:
            min_lon, min_lat, max_lon, max_lat = (float(p) for p in parts)
        except ValueError:
            raise forms.ValidationError("bbox values must be numbers")
        if not (-180 <= min_lon <= 180 and -180 <= max_lon <= 180):
            raise forms.ValidationError("bbox longitudes must be between -180 and 180")
        if not (-90 <= min_lat <= 90 and -90 <= max_lat <= 90):
            raise forms.ValidationError("bbox latitudes must be between -90 and 90")
        if min_lat > max_lat:
            raise forms.ValidationError("bbox min_lat must not exceed max_lat")
        if min_lon > max_lon:
            # A box crossing the antimeridian (e.g. 170 to -170) isn't supported yet.
            raise forms.ValidationError(
                "bbox min_lon must not exceed max_lon (antimeridian boxes unsupported)"
            )
        return (min_lon, min_lat, max_lon, max_lat)


class BBoxFilter(filters.Filter):
    field_class = BBoxField


class EarthquakeFilterForm(forms.Form):
    """Cross-field checks that a single field can't do."""

    def clean(self):
        data = super().clean()
        for low, high in (("start", "end"), ("min_mag", "max_mag")):
            if (
                data.get(low) is not None
                and data.get(high) is not None
                and data[low] > data[high]
            ):
                self.add_error(high, f"must not be less than {low}")
        return data


class EarthquakeFilter(filters.FilterSet):
    start = filters.IsoDateTimeFilter(
        field_name="time", lookup_expr="gte", help_text="Events at or after (ISO 8601)"
    )
    end = filters.IsoDateTimeFilter(
        field_name="time", lookup_expr="lt", help_text="Events before (ISO 8601)"
    )
    updated_since = filters.IsoDateTimeFilter(
        field_name="updated",
        lookup_expr="gte",
        help_text="Only events USGS changed at or after this time: lets clients sync from us incrementally",
    )
    min_mag = filters.NumberFilter(
        field_name="mag", lookup_expr="gte", min_value=-2, max_value=10
    )
    max_mag = filters.NumberFilter(
        field_name="mag", lookup_expr="lte", min_value=-2, max_value=10
    )
    status = filters.ChoiceFilter(
        choices=[
            (EventStatus.AUTOMATIC, "Automatic"),
            (EventStatus.REVIEWED, "Reviewed"),
        ]
    )
    type = filters.CharFilter(
        method="filter_type", max_length=32, help_text="e.g. earthquake, quarry blast"
    )
    bbox = BBoxFilter(method="filter_bbox", help_text="min_lon,min_lat,max_lon,max_lat")

    class Meta:
        model = Earthquake
        fields = []  # all filters are declared explicitly above
        form = EarthquakeFilterForm

    def filter_type(self, queryset, name, value):
        return queryset.filter(
            event_type=value.strip().lower()
        )  # stored lowercase: exact match keeps index use

    def filter_bbox(self, queryset, name, value):
        min_lon, min_lat, max_lon, max_lat = value
        return queryset.filter(
            longitude__gte=min_lon,
            longitude__lte=max_lon,
            latitude__gte=min_lat,
            latitude__lte=max_lat,
        )
