import argparse
import json
import logging
import os
import re
import time
from notion_client import Client
import requests
from requests.utils import cookiejar_from_dict
from http.cookies import SimpleCookie
from datetime import datetime
import hashlib

from utils import get_callout, get_date, get_file, get_heading, get_icon, get_multi_select, get_number, get_quote, get_rich_text, get_select, get_table_of_contents, get_title, get_url

WEREAD_URL = "https://weread.qq.com/"
WEREAD_NOTEBOOKS_URL = "https://i.weread.qq.com/user/notebooks"
WEREAD_BOOKMARKLIST_URL = "https://i.weread.qq.com/book/bookmarklist"
WEREAD_CHAPTER_INFO = "https://i.weread.qq.com/book/chapterInfos"
WEREAD_READ_INFO_URL = "https://i.weread.qq.com/book/readinfo"
WEREAD_REVIEW_LIST_URL = "https://i.weread.qq.com/review/list"
WEREAD_BOOK_INFO = "https://i.weread.qq.com/book/info"


def parse_cookie_string(cookie_string):
    cookie = SimpleCookie()
    cookie.load(cookie_string)
    cookies_dict = {}
    cookiejar = None
    for key, morsel in cookie.items():
        cookies_dict[key] = morsel.value
        cookiejar = cookiejar_from_dict(cookies_dict, cookiejar=None, overwrite=True)
    return cookiejar


def get_bookmark_list(bookId):
    """获取我的划线"""
    params = dict(bookId=bookId)
    r = session.get(WEREAD_BOOKMARKLIST_URL, params=params)
    if r.ok:
        updated = r.json().get("updated")
        updated = sorted(
            updated,
            key=lambda x: (x.get("chapterUid", 1), int(x.get("range").split("-")[0])),
        )
        return r.json()["updated"]
    return None


def get_read_info(bookId):
    params = dict(bookId=bookId, readingDetail=1, readingBookIndex=1, finishedDate=1)
    r = session.get(WEREAD_READ_INFO_URL, params=params)
    if r.ok:
        return r.json()
    return None


def get_bookinfo(bookId):
    """获取书的详情"""
    params = dict(bookId=bookId)
    r = session.get(WEREAD_BOOK_INFO, params=params)
    isbn = ""
    if r.ok:
        data = r.json()
        isbn = data["isbn"]
        newRating = data["newRating"] / 1000
        return (isbn, newRating)
    else:
        print(f"get {bookId} book info failed")
        return ("", 0)


def get_review_list(bookId):
    """获取笔记"""
    params = dict(bookId=bookId, listType=11, mine=1, syncKey=0)
    r = session.get(WEREAD_REVIEW_LIST_URL, params=params)
    reviews = r.json().get("reviews")
    summary = list(filter(lambda x: x.get("review").get("type") == 4, reviews))
    reviews = list(filter(lambda x: x.get("review").get("type") == 1, reviews))
    reviews = list(map(lambda x: x.get("review"), reviews))
    reviews = list(map(lambda x: {**x, "markText": x.pop("content")}, reviews))
    return summary, reviews


def check(bookId):
    """检查是否已经插入过 如果已经插入了就删除"""
    time.sleep(0.3)
    filter = {"property": "BookId", "rich_text": {"equals": bookId}}
    response = client.databases.query(database_id=database_id, filter=filter)
    for result in response["results"]:
        time.sleep(0.3)
        client.blocks.delete(block_id=result["id"])


def get_chapter_info(bookId):
    """获取章节信息"""
    body = {"bookIds": [bookId], "synckeys": [0], "teenmode": 0}
    r = session.post(WEREAD_CHAPTER_INFO, json=body)
    if (
        r.ok
        and "data" in r.json()
        and len(r.json()["data"]) == 1
        and "updated" in r.json()["data"][0]
    ):
        update = r.json()["data"][0]["updated"]
        return {item["chapterUid"]: item for item in update}
    return None


def insert_to_notion(bookName, bookId, cover, sort, author, isbn, rating, categories):
    """插入到notion"""
    time.sleep(0.3)
    parent = {"database_id": database_id, "type": "database_id"}
    properties = {
        "BookName":get_title(bookName),
        "BookId": get_rich_text(bookId),
        "ISBN": get_rich_text(isbn),
        "URL": get_url(f"https://weread.qq.com/web/reader/{calculate_book_str_id(bookId)}"),
        "Author": get_rich_text(author),
        "Sort": get_number(sort),
        "Recommended": get_number(rating),
        "Cover": get_file(cover),
    }
    if categories != None:
        properties["Categories"] =get_multi_select(categories)
    read_info = get_read_info(bookId=bookId)
    if read_info != None:
        markedStatus = read_info.get("markedStatus", 0)
        readingTime = read_info.get("readingTime", 0)
        readingProgress = read_info.get("readingProgress", 0)
        if readingTime // 60 > 0:
            format_time = "已读"
        else:
            format_time = ""
        hour = readingTime // 3600
        if hour > 0:
            format_time += f"{hour}小时"
        minutes = readingTime % 3600 // 60
        if minutes > 0:
            format_time += f"{minutes}分钟"
        properties["Status"] = get_select("读完" if markedStatus == 4 else "在读")
        properties["ReadingTime"] = get_rich_text(format_time)
        properties["Progress"] = get_number(readingProgress)
        if "finishedDate" in read_info:
            properties["Finish_Date"] = get_date(datetime.utcfromtimestamp(
                        read_info.get("finishedDate")
                    ).strftime("%Y-%m-%d %H:%M:%S"))

    if cover.startswith("http"):
        icon = get_icon(cover)
    # notion api 限制100个block
    response = client.pages.create(parent=parent, icon=icon, properties=properties)
    id = response["id"]
    return id


def add_children(id, children):
    results = []
    for i in range(0, len(children) // 100 + 1):
        time.sleep(0.3)
        response = client.blocks.children.append(
            block_id=id, children=children[i * 100 : (i + 1) * 100]
        )
        results.extend(response.get("results"))
    return results if len(results) == len(children) else None


def add_grandchild(grandchild, results):
    for key, value in grandchild.items():
        time.sleep(0.3)
        id = results[key].get("id")
        client.blocks.children.append(block_id=id, children=[value])


def get_notebooklist():
    """获取笔记本列表"""
    r = session.get(WEREAD_NOTEBOOKS_URL)
    if r.ok:
        data = r.json()
        books = data.get("books")
        books.sort(key=lambda x: x["sort"])
        return books
    else:
        print(r.text)
    return None


def get_sort():
    """获取database中的最新时间"""
    '''也就是说如果没有在本书继续做笔记，本书的sort就不会增加，否则增长；'''
    '''这样在插入笔记时，就会先进行比较，若sort未增加，就会后续的修改记录，否则才会补充修改笔记'''
    '''相当于减少不必要的程序运行'''
    filter = {"property": "Sort", "number": {"is_not_empty": True}}
    sorts = [
        {
            "property": "Sort",
            "direction": "descending",
        }
    ]
    response = client.databases.query(
        database_id=database_id, filter=filter, sorts=sorts, page_size=1
    )
    if len(response.get("results")) == 1:
        return response.get("results")[0].get("properties").get("Sort").get("number")
    return 0


def get_children(chapter, summary, bookmark_list):
    children = []
    grandchild = {}
    if chapter != None:
        # 添加目录
        children.append(get_table_of_contents())
        d = {}
        for data in bookmark_list:
            chapterUid = data.get("chapterUid", 1)
            if chapterUid not in d:
                d[chapterUid] = []
            d[chapterUid].append(data)
        for key, value in d.items():
            if key in chapter:
                # 添加章节
                children.append(
                    get_heading(
                        chapter.get(key).get("level"), chapter.get(key).get("title")
                    )
                )
            for i in value:
                if data.get("reviewId") == None and "style" in i and "colorStyle" in i:
                    if i.get("style") not in styles:
                        continue
                    if i.get("colorStyle") not in colors:
                        continue
                markText = i.get("markText")
                for j in range(0, len(markText) // 2000 + 1):
                    children.append(
                        get_callout(
                            markText[j * 2000 : (j + 1) * 2000],
                            i.get("style"),
                            i.get("colorStyle"),
                            i.get("reviewId"),
                        )
                    )
                if i.get("abstract") != None and i.get("abstract") != "":
                    quote = get_quote(i.get("abstract"))
                    grandchild[len(children) - 1] = quote

    else:
        # 如果没有章节信息
        for data in bookmark_list:
            if (
                data.get("reviewId") == None
                and "style" in data
                and "colorStyle" in data
            ):
                if data.get("style") not in styles:
                    continue
                if data.get("colorStyle") not in colors:
                    continue
            markText = data.get("markText")
            for i in range(0, len(markText) // 2000 + 1):
                children.append(
                    get_callout(
                        markText[i * 2000 : (i + 1) * 2000],
                        data.get("style"),
                        data.get("colorStyle"),
                        data.get("reviewId"),
                    )
                )
    if summary != None and len(summary) > 0:
        children.append(get_heading(1, "点评"))
        for i in summary:
            content = i.get("review").get("content")
            for j in range(0, len(content) // 2000 + 1):
                children.append(
                    get_callout(
                        content[j * 2000 : (j + 1) * 2000],
                        i.get("style"),
                        i.get("colorStyle"),
                        i.get("review").get("reviewId"),
                    )
                )
    return children, grandchild


def transform_id(book_id):
    id_length = len(book_id)

    if re.match("^\d*$", book_id):
        ary = []
        for i in range(0, id_length, 9):
            ary.append(format(int(book_id[i : min(i + 9, id_length)]), "x"))
        return "3", ary

    result = ""
    for i in range(id_length):
        result += format(ord(book_id[i]), "x")
    return "4", [result]


def calculate_book_str_id(book_id):
    md5 = hashlib.md5()
    md5.update(book_id.encode("utf-8"))
    digest = md5.hexdigest()
    result = digest[0:3]
    code, transformed_ids = transform_id(book_id)
    result += code + "2" + digest[-2:]

    for i in range(len(transformed_ids)):
        hex_length_str = format(len(transformed_ids[i]), "x")
        if len(hex_length_str) == 1:
            hex_length_str = "0" + hex_length_str

        result += hex_length_str + transformed_ids[i]

        if i < len(transformed_ids) - 1:
            result += "g"

    if len(result) < 20:
        result += digest[0 : 20 - len(result)]

    md5 = hashlib.md5()
    md5.update(result.encode("utf-8"))
    result += md5.hexdigest()[0:3]
    return result


def download_image(url, save_dir="cover"):
    # 确保目录存在，如果不存在则创建
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    # 获取文件名，使用 URL 最后一个 '/' 之后的字符串
    file_name = url.split("/")[-1] + ".jpg"
    save_path = os.path.join(save_dir, file_name)

    # 检查文件是否已经存在，如果存在则不进行下载
    if os.path.exists(save_path):
        print(f"File {file_name} already exists. Skipping download.")
        return save_path

    response = requests.get(url, stream=True)
    if response.status_code == 200:
        with open(save_path, "wb") as file:
            for chunk in response.iter_content(chunk_size=128):
                file.write(chunk)
        print(f"Image downloaded successfully to {save_path}")
    else:
        print(f"Failed to download image. Status code: {response.status_code}")
    return save_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("weread_cookie")
    parser.add_argument("notion_token")
    parser.add_argument("database_id")
    parser.add_argument("ref")
    parser.add_argument("repository")
    parser.add_argument("--styles", nargs="+", type=int, help="划线样式")
    parser.add_argument("--colors", nargs="+", type=int, help="划线颜色")
    options = parser.parse_args()
    weread_cookie = options.weread_cookie
    database_id = options.database_id
    notion_token = options.notion_token
    ref = options.ref
    branch = ref.split("/")[-1]
    repository = options.repository
    styles = options.styles
    colors = options.colors
    session = requests.Session()
    session.cookies = parse_cookie_string(weread_cookie)
    client = Client(auth=notion_token, log_level=logging.ERROR)
    session.get(WEREAD_URL)
    latest_sort = get_sort()
    books = get_notebooklist() # books返回回来其实是个列表，列表形式如下
    '''
    如，形式如下：
    [{'bookId': '23071792', 'book': {'bookId': '23071792', 'title': '简单统计学：如何轻松识破一本正经的胡说八道', 'author': '加里·史密斯', 'translator': '刘清山', 'cover': 'https://cdn.weread.qq.com/weread/cover/11/YueWen_23071792/s_YueWen_23071792.jpg', 'version': 1624236154, 'format': 'epub', 'type': 0, 'price': 14.99, 'originalPrice': 0, 'soldout': 0, 'bookStatus': 1, 'payType': 1048577, 'centPrice': 1499, 'finished': 1, 'maxFreeChapter': 5, 'free': 0, 'mcardDiscount': 0, 'ispub': 1, 'extra_type': 5, 'cpid': 9555120, 'publishTime': '2018-01-01 00:00:00', 'categories': [{'categoryId': 1100000, 'subCategoryId': 1100001, 'categoryType': 0, 'title': '经济理财-财经'}], 'hasLecture': 0, 'lastChapterIdx': 23, 'paperBook': {'skuId': '12246729'}, 'maxFreeInfo': {'maxFreeChapterIdx': 5, 'maxFreeChapterUid': 5, 'maxFreeChapterRatio': 15}, 'copyrightChapterUids': [2], 'hasKeyPoint': True, 'blockSaveImg': 0, 'language': 'zh', 'hideUpdateTime': False, 'isEPUBComics': 0, 'webBookControl': 0}, 'reviewCount': 0, 'reviewLikeCount': 0, 'reviewCommentCount': 0, 'noteCount': 157, 'bookmarkCount': 0, 'sort': 1566838330}, \
    {'bookId': '25306870', 'book': {'bookId': '25306870', 'title': '能力陷阱', 'author': '埃米尼亚·伊贝拉', 'translator': '王臻', 'cover': 'https://wfqqreader-1252317822.image.myqcloud.com/cover/870/25306870/s_25306870.jpg', 'version': 20658450, 'format': 'epub', 'type': 0, 'price': 44.99, 'originalPrice': 0, 'soldout': 0, 'bookStatus': 1, 'payType': 1048577, 'centPrice': 4499, 'finished': 1, 'maxFreeChapter': 10, 'free': 0, 'mcardDiscount': 0, 'ispub': 1, 'extra_type': 1, 'cpid': 16669091, 'publishTime': '2019-05-01 00:00:00', 'categories': [{'categoryId': 1100000, 'subCategoryId': 1100002, 'categoryType': 0, 'title': '经济理财-管理'}], 'hasLecture': 0, 'lastChapterIdx': 43, 'paperBook': {'skuId': ''}, 'maxFreeInfo': {'maxFreeChapterIdx': 10, 'maxFreeChapterUid': 52, 'maxFreeChapterRatio': 47}, 'copyrightChapterUids': [44], 'hasKeyPoint': True, 'blockSaveImg': 0, 'language': 'zh', 'hideUpdateTime': False, 'isEPUBComics': 0, 'webBookControl': 0}, 'reviewCount': 0, 'reviewLikeCount': 0, 'reviewCommentCount': 0, 'noteCount': 13, 'bookmarkCount': 0, 'sort': 1567091406},\
    {'bookId': '842520', 'book': {'bookId': '842520', 'title': '独立日1：用一间书房 抵抗全世界', 'author': '魏小河', 'cover': 'https://cdn.weread.qq.com/weread/cover/37/YueWen_842520/s_YueWen_842520.jpg', 'version': 716676880, 'format': 'epub', 'type': 0, 'price': 28.8, 'originalPrice': 0, 'soldout': 1, 'bookStatus': 1, 'payType': 1048577, 'centPrice': 2880, 'finished': 1, 'maxFreeChapter': 24, 'free': 0, 'mcardDiscount': 0, 'ispub': 1, 'extra_type': 5, 'cpid': 17629620, 'publishTime': '2015-06-01 00:00:00', 'categories': [{'categoryId': 300000, 'subCategoryId': 300005, 'categoryType': 0, 'title': '文学-散文杂著'}], 'hasLecture': 0, 'shouldHideTTS': 1, 'lastChapterIdx': 101, 'paperBook': {'skuId': '11727221'}, 'maxFreeInfo': {'maxFreeChapterIdx': 24, 'maxFreeChapterUid': 24, 'maxFreeChapterRatio': 48}, 'copyrightChapterUids': [2], 'hasKeyPoint': False, 'blockSaveImg': 0, 'language': 'zh', 'hideUpdateTime': True, 'isEPUBComics': 0, 'webBookControl': 0}, 'reviewCount': 0, 'reviewLikeCount': 0, 'reviewCommentCount': 0, 'noteCount': 3, 'bookmarkCount': 0, 'sort': 1569951252}, \
    {'bookId': '22806949', 'book': {'bookId': '22806949', 'title': '父与子的编程之旅：与小卡特一起学Python', 'author': '沃伦·桑德 卡特·桑德', 'translator': '苏金国,易郑超', 'cover': 'https://wfqqreader-1252317822.image.myqcloud.com/cover/949/22806949/s_22806949.jpg', 'version': 1341868227, 'format': 'epub', 'type': 0, 'price': 39.99, 'originalPrice': 0, 'soldout': 0, 'bookStatus': 1, 'payType': 1048577, 'centPrice': 3999, 'finished': 1, 'maxFreeChapter': 45, 'free': 0, 'mcardDiscount': 0, 'ispub': 1, 'extra_type': 1, 'cpid': 5256588, 'publishTime': '2014-11-01 00:00:00', 'categories': [{'categoryId': 700000, 'subCategoryId': 700003, 'categoryType': 0, 'title': '计算机-计算机综合'}], 'hasLecture': 0, 'lastChapterIdx': 255, 'paperBook': {'skuId': ''}, 'maxFreeInfo': {'maxFreeChapterIdx': 45, 'maxFreeChapterUid': 45, 'maxFreeChapterRatio': 25}, 'copyrightChapterUids': [2], 'hasKeyPoint': True, 'blockSaveImg': 1, 'language': 'zh', 'hideUpdateTime': False, 'isEPUBComics': 0, 'webBookControl': 0}, 'reviewCount': 0, 'reviewLikeCount': 0, 'reviewCommentCount': 0, 'noteCount': 10, 'bookmarkCount': 0, 'sort': 1572411009},\
    {'bookId': '26720007', 'book': {'bookId': '26720007', 'title': '读懂一本书：樊登读书法', 'author': '樊登', 'cover': 'https://cdn.weread.qq.com/weread/cover/34/YueWen_26720007/s_YueWen_26720007.jpg', 'version': 85756318, 'format': 'epub', 'type': 0, 'price': 35.4, 'originalPrice': 0, 'soldout': 0, 'bookStatus': 1, 'payType': 4097, 'centPrice': 3540, 'finished': 1, 'maxFreeChapter': 7, 'free': 0, 'mcardDiscount': 0, 'ispub': 1, 'extra_type': 5, 'cpid': 4525313, 'publishTime': '2019-10-01 00:00:00', 'categories': [{'categoryId': 1000000, 'subCategoryId': 1000002, 'categoryType': 0, 'title': '个人成长-励志成长'}], 'hasLecture': 0, 'lastChapterIdx': 50, 'paperBook': {'skuId': '12726546'}, 'maxFreeInfo': {'maxFreeChapterIdx': 7, 'maxFreeChapterUid': 56, 'maxFreeChapterRatio': 42}, 'copyrightChapterUids': [51], 'hasKeyPoint': True, 'blockSaveImg': 0, 'language': 'zh', 'hideUpdateTime': False, 'isEPUBComics': 0, 'webBookControl': 0}, 'reviewCount': 0, 'reviewLikeCount': 0, 'reviewCommentCount': 0, 'noteCount': 7, 'bookmarkCount': 0, 'sort': 1573603472}, \
    {'bookId': '603241', 'book': {'bookId': '603241', 'title': '数据挖掘与数据化运营实战：思路、方法、技巧与应用', 'author': '卢辉', 'cover': 'https://wfqqreader-1252317822.image.myqcloud.com/cover/241/603241/s_603241.jpg', 'version': 1881954565, 'format': 'epub', 'type': 0, 'price': 25.0, 'originalPrice': 0, 'soldout': 1, 'bookStatus': 1, 'payType': 1, 'centPrice': 2500, 'finished': 1, 'maxFreeChapter': 20, 'free': 0, 'mcardDiscount': 0, 'ispub': 1, 'extra_type': 1, 'cpid': 1000000171, 'publishTime': '2013-06-01 00:00:00', 'categories': [{'categoryId': 700000, 'subCategoryId': 700006, 'categoryType': 0, 'title': '计算机-数据库'}], 'hasLecture': 0, 'lastChapterIdx': 279, 'paperBook': {'skuId': '11252775'}, 'maxFreeInfo': {'maxFreeChapterIdx': 20, 'maxFreeChapterUid': 20, 'maxFreeChapterRatio': 43}, 'copyrightChapterUids': [2], 'hasKeyPoint': True, 'blockSaveImg': 0, 'language': 'zh', 'hideUpdateTime': True, 'isEPUBComics': 0, 'webBookControl': 0}, 'reviewCount': 0, 'reviewLikeCount': 0, 'reviewCommentCount': 0, 'noteCount': 1, 'bookmarkCount': 0, 'sort': 1574942067}, \
    {'bookId': '22291932', 'book': {'bookId': '22291932', 'title': '丰田一页纸极简思考法', 'author': '浅田卓', 'translator': '侯月', 'cover': 'https://wfqqreader-1252317822.image.myqcloud.com/cover/932/22291932/s_22291932.jpg', 'version': 1888460478, 'format': 'epub', 'type': 0, 'price': 19.9, 'originalPrice': 0, 'soldout': 0, 'bookStatus': 1, 'payType': 1048577, 'centPrice': 1990, 'finished': 1, 'maxFreeChapter': 6, 'free': 0, 'mcardDiscount': 0, 'ispub': 1, 'extra_type': 1, 'cpid': 9838507, 'publishTime': '2018-05-01 00:00:00', 'categories': [{'categoryId': 1100000, 'subCategoryId': 1100002, 'categoryType': 0, 'title': '经济理财-管理'}], 'hasLecture': 0, 'lastChapterIdx': 37, 'paperBook': {'skuId': '12351790'}, 'maxFreeInfo': {'maxFreeChapterIdx': 6, 'maxFreeChapterUid': 6, 'maxFreeChapterRatio': 35}, 'copyrightChapterUids': [2], 'hasKeyPoint': True, 'blockSaveImg': 0, 'language': 'zh', 'hideUpdateTime': False, 'isEPUBComics': 0, 'webBookControl': 0}, 'reviewCount': 0, 'reviewLikeCount': 0, 'reviewCommentCount': 0, 'noteCount': 5, 'bookmarkCount': 0, 'sort': 1574959640}, \
    {'bookId': '26454161', 'book': {'bookId': '26454161', 'title': '万物发明指南', 'author': '瑞安·诺思', 'translator': '王乔琦', 'cover': 'https://cdn.weread.qq.com/weread/cover/93/YueWen_26454161/s_YueWen_26454161.jpg', 'version': 2106389050, 'format': 'epub', 'type': 0, 'price': 46.8, 'originalPrice': 0, 'soldout': 0, 'bookStatus': 1, 'payType': 1048577, 'centPrice': 4680, 'finished': 1, 'maxFreeChapter': 18, 'free': 0, 'mcardDiscount': 0, 'ispub': 1, 'extra_type': 5, 'cpid': 4525313, 'publishTime': '2019-09-01 00:00:00', 'categories': [{'categoryId': 1500000, 'subCategoryId': 1500005, 'categoryType': 0, 'title': '科学技术-自然科学'}], 'hasLecture': 0, 'lastChapterIdx': 56, 'paperBook': {'skuId': '12698994'}, 'maxFreeInfo': {'maxFreeChapterIdx': 18, 'maxFreeChapterUid': 18, 'maxFreeChapterRatio': 53}, 'copyrightChapterUids': [2], 'hasKeyPoint': True, 'blockSaveImg': 0, 'language': 'zh', 'hideUpdateTime': False, 'isEPUBComics': 0, 'webBookControl': 0}, 'reviewCount': 0, 'reviewLikeCount': 0, 'reviewCommentCount': 0, 'noteCount': 2, 'bookmarkCount': 0, 'sort': 1575503418},]
    '''
    if books != None:
        for index, book in enumerate(books):
            sort = book["sort"]
            '''这里，其实就是将实时的sort记录时间，与notion中记录的sort时间latest_sort比较'''
            '''如果，未增长，就没必要继续后面的程序，即continue；否则，再修改本书笔记'''
            if sort <= latest_sort:
                continue
            book = book.get("book")
            title = book.get("title")
            cover = book.get("cover")
            if book.get("author") == "公众号" and book.get("cover").endswith("/0"):
                cover += ".jpg"
            if cover.startswith("http") and not cover.endswith(".jpg"):
                path = download_image(cover)
                cover = (
                    f"https://raw.githubusercontent.com/{repository}/{branch}/{path}"
                )
            bookId = book.get("bookId")
            author = book.get("author")
            categories = book.get("categories")
            if categories != None:
                categories = [x["title"] for x in categories]
            print(f"正在同步 {title} ,一共{len(books)}本，当前是第{index+1}本。")
            check(bookId)
            isbn, rating = get_bookinfo(bookId)
            id = insert_to_notion(
                title, bookId, cover, sort, author, isbn, rating, categories
            )
            chapter = get_chapter_info(bookId)
            bookmark_list = get_bookmark_list(bookId)
            summary, reviews = get_review_list(bookId)
            bookmark_list.extend(reviews)
            bookmark_list = sorted(
                bookmark_list,
                key=lambda x: (
                    x.get("chapterUid", 1),
                    0
                    if (x.get("range", "") == "" or x.get("range").split("-")[0] == "")
                    else int(x.get("range").split("-")[0]),
                ),
            )
            children, grandchild = get_children(chapter, summary, bookmark_list)
            results = add_children(id, children)
            if len(grandchild) > 0 and results != None:
                add_grandchild(grandchild, results)
