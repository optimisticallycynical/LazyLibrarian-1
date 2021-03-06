import os
import re
import lazylibrarian
import urllib2
import socket
import lib.zipfile as zipfile
from shutil import copyfile
from lazylibrarian import logger, database
from lazylibrarian.bookwork import setWorkPages
from lazylibrarian.cache import cache_cover
from lazylibrarian.gr import GoodReads
from lib.fuzzywuzzy import fuzz
from xml.etree import ElementTree
from lib.mobi import Mobi
from lazylibrarian.common import USER_AGENT, opf_file
from lazylibrarian.formatter import plural, is_valid_isbn, is_valid_booktype, getList, unaccented, replace_all
from lazylibrarian.importer import addAuthorToDB, update_totals


def get_book_info(fname):
    # only handles epub, mobi, azw3 and opf for now,
    # for pdf see notes below
    res = {}
    extn = os.path.splitext(fname)[1]
    if not extn:
        return res
    if extn == ".mobi" or extn == ".azw3":
        res['type'] = extn[1:]
        try:
            book = Mobi(fname)
            book.parse()
        except Exception as e:
            logger.debug('Unable to parse mobi in %s, %s' % (fname, str(e)))
            return res
        res['creator'] = book.author()
        res['title'] = book.title()
        res['language'] = book.language()
        res['identifier'] = book.isbn()
        return res

        """
        # none of the pdfs in my library had language,isbn
        # most didn't have author, or had the wrong author
        # (author set to publisher, or software used)
        # so probably not much point in looking at pdfs
        #
        if (extn == ".pdf"):
            pdf = PdfFileReader(open(fname, "rb"))
            txt = pdf.getDocumentInfo()
            # repackage the data here to get components we need
            res = {}
            for s in ['title','language','creator']:
                res[s] = txt[s]
            res['identifier'] = txt['isbn']
            res['type'] = "pdf"
            return res
        """
    elif extn == ".epub":
        res['type'] = "epub"

        # prepare to read from the .epub file
        try:
            zipdata = zipfile.ZipFile(fname)
        except Exception as e:
            logger.debug('Unable to parse zipfile %s, %s' % (fname, str(e)))
            return res

        # find the contents metafile
        txt = zipdata.read('META-INF/container.xml')
        tree = ElementTree.fromstring(txt)
        n = 0
        cfname = ""
        if not len(tree):
            return res

        while n < len(tree[0]):
            att = tree[0][n].attrib
            if 'full-path' in att:
                cfname = att['full-path']
                break
            n = n + 1

        # grab the metadata block from the contents metafile
        txt = zipdata.read(cfname)

    elif extn == ".opf":
        res['type'] = "opf"
        txt = open(fname).read()
        # sanitize any unmatched html tags or ElementTree won't parse
        dic = {'<br>': '', '</br>': ''}
        txt = replace_all(txt, dic)

    # repackage epub or opf metadata
    try:
        tree = ElementTree.fromstring(txt)
    except Exception as e:
        logger.error("Error parsing metadata from %s, %s" % (fname, str(e)))
        return res

    if not len(tree):
        return res
    n = 0
    while n < len(tree[0]):
        tag = str(tree[0][n].tag).lower()
        if '}' in tag:
            tag = tag.split('}')[1]
            txt = tree[0][n].text
            attrib = str(tree[0][n].attrib).lower()
            if 'title' in tag:
                res['title'] = txt
            elif 'language' in tag:
                res['language'] = txt
            elif 'creator' in tag:
                res['creator'] = txt
            elif 'identifier' in tag and 'isbn' in attrib:
                if is_valid_isbn(txt):
                    res['identifier'] = txt
        n = n + 1
    return res


def find_book_in_db(myDB, author, book):
    # PAB fuzzy search for book in library, return LL bookid if found or zero
    # if not, return bookid to more easily update status
    # prefer an exact match on author & book
    match = myDB.match('SELECT BookID FROM books where AuthorName="%s" and BookName="%s"' %
                       (author.replace('"', '""'), book.replace('"', '""')))
    if match:
        logger.debug('Exact match [%s]' % book)
        return match['BookID']
    else:
        # No exact match
        # Try a more complex fuzzy match against each book in the db by this author
        # Using hard-coded ratios for now, ratio high (>90), partial_ratio lower (>75)
        # These are results that work well on my library, minimal false matches and no misses
        # on books that should be matched
        # Maybe make ratios configurable in config.ini later

        books = myDB.select('SELECT BookID,BookName FROM books where AuthorName="%s"' % author.replace('"', '""'))

        best_ratio = 0
        best_partial = 0
        best_partname = 0
        ratio_name = ""
        partial_name = ""
        partname_name = ""
        ratio_id = 0
        partial_id = 0
        partname_id = 0
        partname = 0

        book_lower = unaccented(book.lower())
        book_partname = ''
        if ':' in book_lower:
            book_partname = book_lower.split(':')[0]

        for a_book in books:
            # tidy up everything to raise fuzziness scores
            # still need to lowercase for matching against partial_name later on
            a_book_lower = unaccented(a_book['BookName'].lower())
            #
            ratio = fuzz.ratio(book_lower, a_book_lower)
            partial = fuzz.partial_ratio(book_lower, a_book_lower)
            if book_partname:
                partname = fuzz.partial_ratio(book_partname, a_book_lower)

            # lose a point for each extra word in the fuzzy matches so we get the closest match
            words = len(getList(book_lower))
            words -= len(getList(a_book_lower))
            ratio -= abs(words)
            partial -= abs(words)

            if ratio > best_ratio:
                best_ratio = ratio
                ratio_name = a_book['BookName']
                ratio_id = a_book['BookID']
            if partial > best_partial:
                best_partial = partial
                partial_name = a_book['BookName']
                partial_id = a_book['BookID']
            if partname > best_partname:
                best_partname = partname
                partname_name = a_book['BookName']
                partname_id = a_book['BookID']

            if partial == best_partial:
                # prefer the match closest to the left, ie prefer starting with a match and ignoring the rest
                # this eliminates most false matches against omnibuses
                # find the position of the shortest string in the longest
                if len(getList(book_lower)) >= len(getList(a_book_lower)):
                    match1 = book_lower.find(a_book_lower)
                else:
                    match1 = a_book_lower.find(book_lower)

                if len(getList(book_lower)) >= len(getList(partial_name.lower())):
                    match2 = book_lower.find(partial_name.lower())
                else:
                    match2 = partial_name.lower().find(book_lower)

                if match1 < match2:
                    logger.debug(
                        "Fuzz left change, prefer [%s] over [%s] for [%s]" %
                        (a_book['BookName'], partial_name, book))
                    best_partial = partial
                    partial_name = a_book['BookName']
                    partial_id = a_book['BookID']

        if best_ratio > 90:
            logger.debug(
                "Fuzz match   ratio [%d] [%s] [%s]" %
                (best_ratio, book, ratio_name))
            return ratio_id
        if best_partial > 75:
            logger.debug(
                "Fuzz match partial [%d] [%s] [%s]" %
                (best_partial, book, partial_name))
            return partial_id
        if best_partname > 95:
            logger.debug(
                "Fuzz match partname [%d] [%s] [%s]" %
                (best_partname, book, partname_name))
            return partname_id

        logger.debug(
            'Fuzz failed [%s - %s] ratio [%d,%s], partial [%d,%s], partname [%d,%s]' %
            (author, book, best_ratio, ratio_name, best_partial, partial_name, best_partname, partname_name))
        return 0


def LibraryScan(startdir=None):
    """ Scan a directory tree adding new books into database
        Return how many books you added """
    destdir = lazylibrarian.DIRECTORY('Destination')
    if not startdir:
        if not destdir:
            return 0
        startdir = destdir

    if not os.path.isdir(startdir):
        logger.warn(
            'Cannot find directory: %s. Not scanning' % startdir)
        return 0

    myDB = database.DBConnection()

    # keep statistics of full library scans
    if startdir == destdir:
        myDB.action('DELETE from stats')

    logger.info('Scanning ebook directory: %s' % startdir)

    new_book_count = 0
    modified_count = 0
    file_count = 0
    author = ""

    if lazylibrarian.FULL_SCAN and startdir == destdir:
        books = myDB.select(
            'select AuthorName, BookName, BookFile, BookID from books where Status="Open"')
        status = lazylibrarian.NOTFOUND_STATUS
        logger.info('Missing books will be marked as %s' % status)
        for book in books:
            bookName = book['BookName']
            bookAuthor = book['AuthorName']
            bookID = book['BookID']
            bookfile = book['BookFile']

            if not(bookfile and os.path.isfile(bookfile)):
                myDB.action('update books set Status="%s" where BookID="%s"' % (status, bookID))
                myDB.action('update books set BookFile="" where BookID="%s"' % bookID)
                logger.warn('Book %s - %s updated as not found on disk' % (bookAuthor, bookName))

    # to save repeat-scans of the same directory if it contains multiple formats of the same book,
    # keep track of which directories we've already looked at
    processed_subdirectories = []

    matchString = ''
    for char in lazylibrarian.EBOOK_DEST_FILE:
        matchString = matchString + '\\' + char
    # massage the EBOOK_DEST_FILE config parameter into something we can use
    # with regular expression matching
    booktypes = ''
    count = -1
    booktype_list = getList(lazylibrarian.EBOOK_TYPE)
    for book_type in booktype_list:
        count += 1
        if count == 0:
            booktypes = book_type
        else:
            booktypes = booktypes + '|' + book_type
    matchString = matchString.replace("\\$\\A\\u\\t\\h\\o\\r", "(?P<author>.*?)").replace(
        "\\$\\T\\i\\t\\l\\e", "(?P<book>.*?)") + '\.[' + booktypes + ']'
    pattern = re.compile(matchString, re.VERBOSE)

    for r, d, f in os.walk(startdir):
        for directory in d[:]:
            # prevent magazine being scanned
            if directory.startswith("_") or directory.startswith("."):
                d.remove(directory)

        for files in f:
            file_count += 1

            if isinstance(r, str):
                r = r.decode(lazylibrarian.SYS_ENCODING)

            subdirectory = r.replace(startdir, '')
            # Added new code to skip if we've done this directory before.
            # Made this conditional with a switch in config.ini
            # in case user keeps multiple different books in the same subdirectory
            if (lazylibrarian.IMP_SINGLEBOOK) and (subdirectory in processed_subdirectories):
                logger.debug("[%s] already scanned" % subdirectory)
            else:
                # If this is a book, try to get author/title/isbn/language
                # if epub or mobi, read metadata from the book
                # If metadata.opf exists, use that allowing it to override
                # embedded metadata. User may have edited metadata.opf
                # to merge author aliases together
                # If all else fails, try pattern match for author/title
                # and look up isbn/lang from LT or GR later
                match = 0
                if is_valid_booktype(files):

                    logger.debug("[%s] Now scanning subdirectory %s" %
                                 (startdir, subdirectory))

                    language = "Unknown"
                    isbn = ""
                    book = ""
                    author = ""
                    extn = os.path.splitext(files)[1]

                    # if it's an epub or a mobi we can try to read metadata from it
                    if (extn == ".epub") or (extn == ".mobi"):
                        book_filename = os.path.join(
                            r.encode(lazylibrarian.SYS_ENCODING), files.encode(lazylibrarian.SYS_ENCODING))

                        try:
                            res = get_book_info(book_filename)
                        except Exception as e:
                            logger.debug('get_book_info failed for %s, %s' % (book_filename, str(e)))
                            res = {}
                        if 'title' in res and 'creator' in res:  # this is the minimum we need
                            match = 1
                            book = res['title']
                            author = res['creator']
                            if 'language' in res:
                                language = res['language']
                            if 'identifier' in res:
                                isbn = res['identifier']
                            if 'type' in res:
                                extn = res['type']

                            logger.debug("book meta [%s] [%s] [%s] [%s] [%s]" %
                                         (isbn, language, author, book, extn))
                        else:

                            logger.debug("Book meta incomplete in %s" % book_filename)

                    # calibre uses "metadata.opf", LL uses "bookname - authorname.opf"
                    # just look for any .opf file in the current directory since we don't know
                    # LL preferred authorname/bookname at this point.
                    # Allow metadata in file to override book contents as may be users pref

                    metafile = opf_file(r)
                    try:
                        res = get_book_info(metafile)
                    except Exception as e:
                        logger.debug('get_book_info failed for %s, %s' % (metafile, str(e)))
                        res = {}
                    if 'title' in res and 'creator' in res:  # this is the minimum we need
                        match = 1
                        book = res['title']
                        author = res['creator']
                        if 'language' in res:
                            language = res['language']
                        if 'identifier' in res:
                            isbn = res['identifier']
                        logger.debug(
                            "file meta [%s] [%s] [%s] [%s]" %
                            (isbn, language, author, book))
                    else:
                        logger.debug("File meta incomplete in %s" % metafile)

                    if not match:  # no author/book from metadata file, and not embedded either
                        match = pattern.match(files)
                        if match:
                            author = match.group("author")
                            book = match.group("book")
                        else:
                            logger.debug("Pattern match failed [%s]" % files)

                    if match:
                        # flag that we found a book in this subdirectory
                        processed_subdirectories.append(subdirectory)

                        # If we have a valid looking isbn, and language != "Unknown", add it to cache
                        if language != "Unknown" and is_valid_isbn(isbn):
                            logger.debug(
                                "Found Language [%s] ISBN [%s]" %
                                (language, isbn))
                            # we need to add it to language cache if not already
                            # there, is_valid_isbn has checked length is 10 or 13
                            if len(isbn) == 10:
                                isbnhead = isbn[0:3]
                            else:
                                isbnhead = isbn[3:6]
                            match = myDB.match('SELECT lang FROM languages where isbn = "%s"' % (isbnhead))
                            if not match:
                                myDB.action('insert into languages values ("%s", "%s")' % (isbnhead, language))
                                logger.debug("Cached Lang [%s] ISBN [%s]" % (language, isbnhead))
                            else:
                                logger.debug("Already cached Lang [%s] ISBN [%s]" % (language, isbnhead))

                        # get authors name in a consistent format
                        if "," in author:  # "surname, forename"
                            words = author.split(',')
                            author = words[1].strip() + ' ' + words[0].strip()  # "forename surname"
                        if author[1] == ' ':
                            author = author.replace(' ', '.')
                            author = author.replace('..', '.')

                        # Check if the author exists, and import the author if not,
                        # before starting any complicated book-name matching to save repeating the search
                        #
                        check_exist_author = myDB.match(
                            'SELECT * FROM authors where AuthorName="%s"' % author.replace('"', '""'))
                        if not check_exist_author and lazylibrarian.ADD_AUTHOR:
                            # no match for supplied author, but we're allowed to
                            # add new ones

                            GR = GoodReads(author)
                            try:
                                author_gr = GR.find_author_id()
                            except Exception as e:
                                logger.warn("Error finding author id for [%s] %s" % (author, str(e)))
                                continue

                            # only try to add if GR data matches found author data
                            if author_gr:
                                authorname = author_gr['authorname']

                                # "J.R.R. Tolkien" is the same person as "J. R. R. Tolkien" and "J R R Tolkien"
                                match_auth = author.replace('.', '_')
                                match_auth = match_auth.replace(' ', '_')
                                match_auth = match_auth.replace('__', '_')
                                match_name = authorname.replace('.', '_')
                                match_name = match_name.replace(' ', '_')
                                match_name = match_name.replace('__', '_')
                                match_name = unaccented(match_name)
                                match_auth = unaccented(match_auth)
                                # allow a degree of fuzziness to cater for different accented character handling.
                                # some author names have accents,
                                # filename may have the accented or un-accented version of the character
                                # The currently non-configurable value of fuzziness might need to go in config
                                # We stored GoodReads unmodified author name in
                                # author_gr, so store in LL db under that
                                # fuzz.ratio doesn't lowercase for us
                                match_fuzz = fuzz.ratio(match_auth.lower(), match_name.lower())
                                if match_fuzz < 90:
                                    logger.debug(
                                        "Failed to match author [%s] fuzz [%d]" %
                                        (author, match_fuzz))
                                    logger.debug(
                                        "Failed to match author [%s] to authorname [%s]" %
                                        (match_auth, match_name))

                                # To save loading hundreds of books by unknown
                                # authors at GR or GB, ignore if author "Unknown"
                                if (author != "Unknown") and (match_fuzz >= 90):
                                    # use "intact" name for author that we stored in
                                    # GR author_dict, not one of the various mangled versions
                                    # otherwise the books appear to be by a different author!
                                    author = author_gr['authorname']
                                    # this new authorname may already be in the
                                    # database, so check again
                                    check_exist_author = myDB.match(
                                        'SELECT * FROM authors where AuthorName="%s"' % author.replace('"', '""'))
                                    if not check_exist_author:
                                        logger.info("Adding new author [%s]" % author)
                                        try:
                                            addAuthorToDB(author)
                                            check_exist_author = myDB.match(
                                                'SELECT * FROM authors where AuthorName="%s"' %
                                                author.replace('"', '""'))
                                        except Exception:
                                            continue

                        # check author exists in db, either newly loaded or already there
                        if not check_exist_author:
                            logger.debug(
                                "Failed to match author [%s] in database" %
                                author)
                        else:
                            # author exists, check if this book by this author is in our database
                            # metadata might have quotes in book name
                            book = book.replace("'", "")
                            bookid = find_book_in_db(myDB, author, book)

                            if bookid:
                                check_status = myDB.match(
                                    'SELECT Status, BookFile from books where BookID="%s"' % bookid)
                                if check_status and check_status['Status'] != 'Open':
                                    # we found a new book
                                    new_book_count += 1
                                    myDB.action(
                                        'UPDATE books set Status="Open" where BookID="%s"' %
                                        bookid)

                                # update book location so we can check if it gets removed
                                # location may have changed since last scan
                                book_filename = os.path.join(r, files)
                                if book_filename != check_status['BookFile']:
                                    modified_count += 1
                                    logger.debug("Updating book location for %s %s" % (author, book))
                                    myDB.action(
                                        'UPDATE books set BookFile="%s" where BookID="%s"' %
                                        (book_filename, bookid))

                                # update cover file to cover.jpg in book folder (if exists)
                                bookdir = os.path.dirname(book_filename)
                                coverimg = os.path.join(bookdir, 'cover.jpg')
                                if os.path.isfile(coverimg):
                                    cachedir = lazylibrarian.CACHEDIR
                                    cacheimg = os.path.join(cachedir, bookid + '.jpg')
                                    copyfile(coverimg, cacheimg)
                            else:
                                logger.debug(
                                    "Failed to match book [%s] by [%s] in database" %
                                    (book, author))

    logger.info("%s/%s new/modified book%s found and added to the database" %
                (new_book_count, modified_count, plural(new_book_count + modified_count)))
    logger.info("%s file%s processed" % (file_count, plural(file_count)))

    # show statistics of full library scans
    if startdir == destdir:
        stats = myDB.match(
            "SELECT sum(GR_book_hits), sum(GR_lang_hits), sum(LT_lang_hits), sum(GB_lang_change), \
                sum(cache_hits), sum(bad_lang), sum(bad_char), sum(uncached), sum(duplicates) FROM stats")
        if stats and stats['sum(GR_book_hits)']:
            # only show stats if new books added
            if lazylibrarian.BOOK_API == "GoogleBooks":
                logger.debug("GoogleBooks was hit %s time%s for books" %
                             (stats['sum(GR_book_hits)'], plural(stats['sum(GR_book_hits)'])))
                logger.debug("GoogleBooks language was changed %s time%s" %
                             (stats['sum(GB_lang_change)'], plural(stats['sum(GB_lang_change)'])))
            if lazylibrarian.BOOK_API == "GoodReads":
                logger.debug("GoodReads was hit %s time%s for books" %
                             (stats['sum(GR_book_hits)'], plural(stats['sum(GR_book_hits)'])))
                logger.debug("GoodReads was hit %s time%s for languages" %
                             (stats['sum(GR_lang_hits)'], plural(stats['sum(GR_lang_hits)'])))
            logger.debug("LibraryThing was hit %s time%s for languages" %
                         (stats['sum(LT_lang_hits)'], plural(stats['sum(LT_lang_hits)'])))
            logger.debug("Language cache was hit %s time%s" %
                         (stats['sum(cache_hits)'], plural(stats['sum(cache_hits)'])))
            logger.debug("Unwanted language removed %s book%s" %
                         (stats['sum(bad_lang)'], plural(stats['sum(bad_lang)'])))
            logger.debug("Unwanted characters removed %s book%s" %
                         (stats['sum(bad_char)'], plural(stats['sum(bad_char)'])))
            logger.debug("Unable to cache %s book%s with missing ISBN" %
                         (stats['sum(uncached)'], plural(stats['sum(uncached)'])))
            logger.debug("Found %s duplicate book%s" %
                         (stats['sum(duplicates)'], plural(stats['sum(duplicates)'])))
            logger.debug("Cache %s hit%s, %s miss" %
                         (lazylibrarian.CACHE_HIT, plural(lazylibrarian.CACHE_HIT), lazylibrarian.CACHE_MISS))
            cachesize = myDB.match("select count('ISBN') as counter from languages")
            logger.debug("ISBN Language cache holds %s entries" % cachesize['counter'])
            nolang = len(myDB.select('select BookID from Books where status="Open" and BookLang="Unknown"'))
            if nolang:
                logger.warn("Found %s book%s in your library with unknown language" % (nolang, plural(nolang)))

        authors = myDB.select('select AuthorID from authors')
        # Update bookcounts for all authors, not just new ones - refresh may have located
        # new books for existing authors especially if switched provider gb/gr
    else:
        # single author/book import
        authors = myDB.select('select AuthorID from authors where AuthorName = "%s"' % author.replace('"', '""'))

    logger.debug('Updating bookcounts for %i author%s' % (len(authors), plural(len(authors))))
    for author in authors:
        update_totals(author['AuthorID'])

    images = myDB.select('select bookid, bookimg, bookname from books where bookimg like "http%"')
    if len(images):
        logger.info("Caching cover%s for %i book%s" % (plural(len(images)), len(images), plural(len(images))))
        for item in images:
            bookid = item['bookid']
            bookimg = item['bookimg']
            bookname = item['bookname']
            newimg = cache_cover(bookid, bookimg)
            if newimg:
                myDB.action('update books set BookImg="%s" where BookID="%s"' % (newimg, bookid))

    images = myDB.select('select AuthorID, AuthorImg, AuthorName from authors where AuthorImg like "http%"')
    if len(images):
        logger.info("Caching image%s for %i author%s" % (plural(len(images)), len(images), plural(len(images))))
        for item in images:
            authorid = item['authorid']
            authorimg = item['authorimg']
            authorname = item['authorname']
            newimg = cache_cover(authorid, authorimg)
            if newimg:
                myDB.action('update authors set AuthorImg="%s" where AuthorID="%s"' % (newimg, authorid))
    setWorkPages()
    logger.info('Library scan complete')
    return new_book_count
