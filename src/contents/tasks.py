import os

import requests

from contentapi.celery import app
from .models import Content
import time

# @app.task(queue="content_pull")
# def pull_and_store_content():
#     # TODO: The design of this celery task is very weird. It's posting the response to localhost:3000.
#     #  which is not ideal
#     url = "https://example.com/api/pull_data"
#     api_url = "http://localhost:3000/contents/"
#     res = requests.get(url).json()
#     for item in res:
#         payload = {**item}
#         requests.post(api_url, json=payload)


base_url = os.getenv("ZELF_BASE_URL")
api_key = os.getenv("API_KEY")


@app.task(queue="content_pull")
def pull_and_store_content():
	content_pull_url = f"{base_url}/api/v1/contents/"
	headers = {"x-api-key": api_key}
	page = 1

	while True:
		try:
			content_url = f"{content_pull_url}?page={page}"
			content_res = requests.get(url=content_url, headers=headers)
			content_res.raise_for_status()
			content_data = content_res.json()

			for content in content_data["data"]:
				Content.objects.get_or_create(
					unique_id=content["unq_external_id"],
					title=content["title"],
					thumbnail_url=content["thumbnail_view_url"],
					author_username=content['author']['unique_name']
				)

			if content_data["pagination"]["next"]:
				page = content_data["pagination"]["next"]
			else:
				break

		except requests.exceptions.RequestException as e:
			print(f"Error pulling content: {e}")
			break


@app.task(queue="post_ai_comments")
def post_ai_comments():
	api_url = f"{base_url}/api/v1/ai_comment/"
	headers = {
		"x-api-key": api_key,
		"Content-Type": "application/json"
	}

	contents = Content.objects.all()
	for content in contents:
		payload = {
			"content_id": content.unique_id,
			"title": content.title,
			"url": content.url,
			"author_username": content.author.username
		}
		try:
			response = requests.post(api_url, json=payload, headers=headers)
			response.raise_for_status()
			ai_comments = response.json()

			for ai_comment in ai_comments:
				post_final_comment.delay(ai_comment["content_id"], ai_comment["comment_text"])

		except requests.exceptions.RequestException as e:
			print(f"Error posting content ID {content.unique_id}: {e}")


@app.task(queue="final_comment_post", bind=True, max_retries=3)
def post_final_comment(self, content_id, comment_text):
	api_url = f"{base_url}/api/v1/final_comment/"
	headers = {
		"x-api-key": api_key,
		"Content-Type": "application/json"
	}

	payload = {
		"content_id": content_id,
		"comment_text": comment_text
	}
	try:
		response = requests.post(api_url, json=payload, headers=headers)
		response.raise_for_status()
		if response.status_code == 200:
			Content.objects.filter(unique_id=content_id).update(is_commented=True)
		print(f"Successfully posted final comment for content ID {content_id}")
	except requests.exceptions.RequestException as e:
		if response.status_code == 503:
			try:
				time.sleep(30)
				self.retry(exc=e, countdown=30)
			except self.MaxRetriesExceededError:
				print(f"Max retries exceeded for content ID {content_id}")
		elif response.status_code == 400 and "This content is not available for commenting" in response.text:
			print(f"Content ID {content_id} is not available for commenting: {e}")
		else:
			print(f"Error posting final comment for content ID {content_id}: {e}")
