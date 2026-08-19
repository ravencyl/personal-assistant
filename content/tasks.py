from celery import shared_task
import feedparser
from django.utils import timezone


@shared_task
def fetch_rss_feeds():
    """抓取所有 RSS 订阅源的最新内容"""
    from .models import RSSFeed, FeedItem

    feeds = RSSFeed.objects.filter(auto_fetch=True)
    total_new = 0

    for feed_config in feeds:
        try:
            parsed = feedparser.parse(feed_config.url)
            for entry in parsed.entries[:20]:  # 最多处理 20 条
                url = entry.get('link', '')
                if not url:
                    continue

                # 检查是否已存在
                if FeedItem.objects.filter(feed=feed_config, url=url).exists():
                    continue

                # 解析发布时间
                published = None
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    from datetime import datetime
                    published = datetime(*entry.published_parsed[:6])
                    if timezone.is_naive(published):
                        published = timezone.make_aware(published)

                FeedItem.objects.create(
                    feed=feed_config,
                    title=entry.get('title', '')[:255],
                    url=url,
                    description=entry.get('summary', ''),
                    content=entry.get('content', [{}])[0].get('value', '') if entry.get('content') else '',
                    published_at=published,
                )
                total_new += 1

            feed_config.last_fetched = timezone.now()
            feed_config.save(update_fields=['last_fetched'])

        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f'Failed to fetch feed {feed_config.name}: {e}')

    return f'Fetched {total_new} new items'
