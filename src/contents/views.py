import logging
from datetime import timedelta

from django.core.paginator import Paginator
from django.db.models import Sum, F, FloatField, Case, When
from django.db.models.functions import Cast
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView


from contents.models import Content, Author, Tag, ContentTag
from contents.serializers import ContentSerializer, ContentPostSerializer

logger = logging.getLogger(__name__)


class ContentAPIView(APIView):
    DEFAULT_PAGE_SIZE = 10
    MAX_PAGE_SIZE = 100

    def get(self, request):
        """
        TODO: Client is complaining about the app performance, the app is loading very slowly, our QA identified that
         this api is slow af. Make the api performant. Need to add pagination. But cannot use rest framework view set.
         As frontend, app team already using this api, do not change the api schema.
         Need to send some additional data as well,
         --------------------------------
         1. Total Engagement = like_count + comment_count + share_count
         2. Engagement Rate = Total Engagement / Views
         Users are complaining these additional data is wrong.
         Need filter support for client side. Add filters for (author_id, author_username, timeframe )
         For timeframe, the content's timestamp must be withing 'x' days.
         Example: api_url?timeframe=7, will get contents that has timestamp now - '7' days
         --------------------------------
         So things to do:
         1. Make the api performant
         2. Fix the additional data point in the schema
            - Total Engagement = like_count + comment_count + share_count
            - Engagement Rate = Total Engagement / Views
            - Tags: List of tags connected with the content
         3. Filter Support for client side
            - author_id: Author's db id
            - author_username: Author's username
            - timeframe: Content that has timestamp: now - 'x' days
            - tag_id: Tag ID
            - title (insensitive match IE: SQL `ilike %text%`)
         4. Must not change the inner api schema
         5. Remove metadata and secret value from schema
         6. Add pagination
            - Should have page number pagination
            - Should have items per page support in query params
            Example: `api_url?items_per_page=10&page=2`
        """
        queryset = Content.objects.select_related('author').prefetch_related(
            'contenttag_set__tag'
        ).annotate(
            total_engagement=F('like_count') + F('comment_count') + F('share_count'),
            engagement_rate=Cast(
                (F('like_count') + F('comment_count') + F('share_count')) /
                Case(
                    When(view_count=0, then=1),
                    default=F('view_count'),
                    output_field=FloatField(),
                ),
                FloatField()
            )
        ).order_by('-id')

        if author_id := request.query_params.get('author_id'):
            queryset = queryset.filter(author_id=author_id)

        if author_username := request.query_params.get('author_username'):
            queryset = queryset.filter(author__username=author_username)

        if timeframe := request.query_params.get('timeframe'):
            try:
                days = int(timeframe)
                cutoff_date = timezone.now() - timedelta(days=days)
                queryset = queryset.filter(timestamp__gte=cutoff_date)
            except (ValueError, TypeError):
                pass

        if tag_id := request.query_params.get('tag_id'):
            queryset = queryset.filter(contenttag__tag_id=tag_id)

        if title := request.query_params.get('title'):
            queryset = queryset.filter(title__icontains=title)

        # Pagination implementation
        try:
            page = int(request.query_params.get('page', 1))
            items_per_page = min(
                int(request.query_params.get('items_per_page', self.DEFAULT_PAGE_SIZE)),
                self.MAX_PAGE_SIZE
            )
        except ValueError:
            page = 1
            items_per_page = self.DEFAULT_PAGE_SIZE

        paginator = Paginator(queryset, items_per_page)
        page_obj = paginator.get_page(page)

        serialized = ContentSerializer(
            [
                {'content': content, 'author': content.author}
                for content in page_obj.object_list
            ],
            many=True
        )

        return Response({
            'data': serialized.data,
            'pagination': {
                'current_page': page_obj.number,
                'total_pages': paginator.num_pages,
                'total_items': paginator.count,
                'items_per_page': items_per_page,
                'has_next': page_obj.has_next(),
                'has_previous': page_obj.has_previous(),
            }
        }, status=status.HTTP_200_OK)

    def post(self, request):
        """
        TODO: This api is very hard to read, and inefficient.
         The users complaining that the contents they are seeing is not being updated.
         Please find out, why the stats are not being updated.
         ------------------
         Things to change:
         1. This api is hard to read, not developer friendly
         2. Support list, make this api accept list of objects and save it
         3. Fix the users complain
        """
        data = request.data if isinstance(request.data, list) else [request.data]
        response_data = []

        for content_data in data:
            serializer = ContentPostSerializer(data=content_data)
            serializer.is_valid(raise_exception=True)
            validated_data = serializer.validated_data

            # Get or create author
            author_data = validated_data['author']
            author_object, _ = Author.objects.update_or_create(
                unique_id=author_data['unique_external_id'],
                defaults={
                    'username': author_data['unique_name'],
                    'name': author_data['full_name'],
                    'url': author_data['url'],
                    'title': author_data['title'],
                }
            )

            # Get or create content with updated stats
            content_object, _ = Content.objects.update_or_create(
                unique_id=validated_data['unq_external_id'],
                defaults={
                    'author': author_object,
                    'title': validated_data.get('title'),
                    'thumbnail_url': validated_data.get('thumbnail_view_url'),
                    'like_count': validated_data['stats']['likes'],
                    'comment_count': validated_data['stats']['comments'],
                    'share_count': validated_data['stats']['shares'],
                    'view_count': validated_data['stats']['views'],
                }
            )

            # Handle tags  with bulk operations
            tags_to_create = []
            content_tags_to_create = []
            existing_tags = {
                tag.name: tag
                for tag in Tag.objects.filter(name__in=validated_data['hashtags'])
            }

            for tag_name in validated_data['hashtags']:
                if tag_name not in existing_tags:
                    tag = Tag(name=tag_name)
                    tags_to_create.append(tag)
                    existing_tags[tag_name] = tag

            # Bulk create new tags
            if tags_to_create:
                Tag.objects.bulk_create(tags_to_create)

            # Prepare content tags
            existing_content_tags = set(
                ContentTag.objects.filter(
                    content=content_object
                ).values_list('tag__name', flat=True)
            )

            for tag_name in validated_data['hashtags']:
                if tag_name not in existing_content_tags:
                    content_tags_to_create.append(
                        ContentTag(
                            tag=existing_tags[tag_name],
                            content=content_object
                        )
                    )

            # Bulk create new content tags
            if content_tags_to_create:
                ContentTag.objects.bulk_create(content_tags_to_create)

            response_data.append({
                'content': content_object,
                'author': author_object,
            })

        serialized = ContentSerializer(response_data, many=True)
        return Response(serialized.data, status=status.HTTP_201_CREATED)


class ContentStatsAPIView(APIView):
    """
    TODO: This api is taking way too much time to resolve.
     Contents that will be fetched using `ContentAPIView`, we need stats for that
     So it must have the same filters as `ContentAPIView`
     Filter Support for client side
            - author_id: Author's db id
            - author_username: Author's username
            - timeframe: Content that has timestamp: now - 'x' days
            - tag_id: Tag ID
            - title (insensitive match IE: SQL `ilike %text%`)
     -------------------------
     Things To do:
     1. Make the api performant
     2. Fix the additional data point (IE: total engagement, total engagement rate)
     3. Filter Support for client side
         - author_id: Author's db id
         - author_id: Author's db id
         - author_username: Author's username
         - timeframe: Content that has timestamp: now - 'x' days
         - tag_id: Tag ID
         - title (insensitive match IE: SQL `ilike %text%`)
     --------------------------
     Bonus: What changes do we need if we want timezone support?
    """
    def get(self, request):
        query_params = request.query_params.dict()
        author_id = query_params.get('author_id')
        author_username = query_params.get('author_username')
        timeframe = query_params.get('timeframe')
        tag_id = query_params.get('tag_id')
        title = query_params.get('title')

        queryset = Content.objects.all()

        if author_id:
            queryset = queryset.filter(author_id=author_id)

        if author_username:
            queryset = queryset.filter(author__username=author_username)

        if timeframe:
            try:
                days = int(timeframe)
                cutoff_date = timezone.now() - timedelta(days=days)
                queryset = queryset.filter(timestamp__gte=cutoff_date)
            except (ValueError, TypeError):
                logging.exception('Invalid timeframe value')
                pass

        if tag_id:
            queryset = queryset.filter(contenttag__tag_id=tag_id)

        if title:
            queryset = queryset.filter(title__icontains=title)

        stats = queryset.aggregate(
            total_likes=Sum('like_count'),
            total_shares=Sum('share_count'),
            total_comments=Sum('comment_count'),
            total_views=Sum('view_count'),
            total_followers=Sum('author__followers'),
            total_contents=Sum(1),
            total_engagement=Sum(F('like_count') + F('share_count') + F('comment_count')),
            total_engagement_rate=Sum(
                (F('like_count') + F('share_count') + F('comment_count')) /
                Case(
                    When(view_count=0, then=1),
                    default=F('view_count'),
                    output_field=FloatField()
                )
            )
        )

        return Response(stats, status=status.HTTP_200_OK)