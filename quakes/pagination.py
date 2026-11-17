from rest_framework.pagination import CursorPagination

"""
This table is continuously ingested (new earthquakes arriving constantly, via your Celery sync tasks). 
With offset pagination, if a new row is inserted while a client is paging through, 
rows shift and the client can see duplicate records. 
Cursor pagination avoids that — it's stable under concurrent writes.
"""


class EarthquakeCursorPagination(CursorPagination):
    ordering = ("-time", "-id")  # matches the partial index quake_visible_time_id_idx
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 500


class RevisionCursorPagination(CursorPagination):
    ordering = ("-updated", "-id")
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 500
