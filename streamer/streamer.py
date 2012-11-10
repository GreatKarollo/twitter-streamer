import sys
import csv as csv_lib
import logging
import time
import datetime
import tweepy
import simplejson as json
import message_recognizers
import utils

logger = logging.getLogger(__name__)

RETRY_LIMIT = 10


def csv_args(value):
    """Parse a CSV string list into a list of strings.

    Used in command line parsing."""
    return map(str, value.split(","))


def _get_version():
    return "0.0.1"


def _init_logger(config, opts):
    from logging import _checkLevel
    FORMAT = "%(asctime)-15s %(message)s"
    level = _checkLevel(opts.log_level.upper())
    logging.basicConfig(format=FORMAT)
    logger.setLevel(level)


class StreamListener(tweepy.StreamListener):
    def __init__(self, opts, api=None):
        super(StreamListener, self).__init__(api=api)
        self.opts = opts
        self.csv_writer = csv_lib.writer(sys.stdout)

        # Create a list of recognizer instances, in decreasing priority order.
        self.recognizers = (
            message_recognizers.DataContainsRecognizer(
                handler_method=self.parse_status_and_dispatch,
                match_string='"in_reply_to_user_id_str":'),

            message_recognizers.DataContainsRecognizer(
                handler_method=self.parse_limit_and_dispatch,
                match_string='"limit":{'),

            message_recognizers.DataContainsRecognizer(
                handler_method=self.parse_warning_and_dispatch,
                match_string='"warning":'),

            #
            # Everything else gets dumped to output...
            message_recognizers.MatchAnyRecognizer(
                handler_method=self.dump_stream_data),
        )

    def dump_with_timestamp(self, text, category="Unknown"):
        print "(%s)--%s--%s" % (category, datetime.datetime.now(), text)

    def dump_stream_data(self, stream_data):
        self.dump_with_timestamp(stream_data)

    def parse_warning_and_dispatch(self, stream_data):
        try:
            self.dump_stream_data(stream_data)
            warning = json.loads(stream_data)['warning']
            return self.on_warning(warning)
        except json.JSONDecodeError as e:
            logger.exception("Exception parsing: %s" % stream_data)
            return False

    def parse_status_and_dispatch(self, stream_data):
        """Parse an incoming status and do something with it.

        TODO: Better output formatting.
        """
        status = tweepy.models.Status.parse(self.api, json.loads(stream_data))
        if self.tweet_matchp(status):
            if self.opts.fields:
                try:
                    csvrow = []
                    for f in self.opts.fields:
                        value = utils.multi_getattr(status, f, 'n/a')
                        try:
                            value = value.encode('utf8')
                        except AttributeError:
                            # Eat the exception, value is already set.
                            pass
                        csvrow.append(value)
                    print self.csv_writer.writerow(csvrow)
                except UnicodeEncodeError as e:
                    logger.warn(f, exc_info=e)
                    pass
            else:
                print stream_data.strip()

        # Parse stream_data, compare tweet timestamp to current time as GMT;
        # This bit does consume some time, so let's not do it unless absolutely 
        # necessary.
        if self.opts.report_lag:
            now = datetime.datetime.utcnow()
            tweepy_status = tweepy.models.Status.parse(self.api, json.loads(stream_data))
            delta = now - tweepy_status.created_at
            if abs(delta.seconds) > self.opts.report_lag:
                # TODO: Gather and report stats on time lag.
                # TODO: Log transitions: lag less than or greater than current
                # # seconds, rising/falling, etc.
                logger.warn("Tweet time and local time differ by %d seconds", delta.seconds)

    def parse_limit_and_dispatch(self, stream_data):
        return self.on_limit(json.loads(stream_data)['limit']['track'])

    def is_retweet(self, tweet):
        return (hasattr(tweet, 'retweeted_status') and tweet.retweeted_status) \
            or tweet.text.startswith('RT ') \
            or ' RT ' in tweet.text

    def tweet_matchp(self, tweet):
        """Return True if tweet matches selection criteria...

        Currently this filters on self.opts.lang if it is not nothing...
        """
        if self.opts.no_retweets and self.is_retweet(tweet):
            return False

        if self.opts.user_lang:
            return tweet.user.lang in self.opts.user_lang
        else:
            return True

    def on_warning(self, warning):
        logger.warn("Warning: code=%s message=%s" % (warning['code'], warning['message']))
        # If code='FALLING_BEHIND' buffer state is in warning['percent_full']

    def on_error(self, status_code):
        logger.error("StreamListener.on_error: %r" % status_code)
        if status_code != 401:
            logger.error(" -- stopping.")
            # Stop on anything other than a 401 error (Unauthorized)
            return False

    def on_timeout(self):
        """Called when there's a timeout in communications.

        Return False to stop processing.
        """
        logger.warn('on_timeout')
        return  ## Continue streaming.

    def on_data(self, data):
        for r in self.recognizers:
            if r.match(data):
                if r.handle_message(data) is False:
                    return False  # Stop streaming
                # Don't execute any other recognizers, and don't call base
                # on_data() because we've already handled the message.
                return
        # Don't execute any of the base class on_data() handlers. 
        return


def process_tweets(config, opts):
    """Set up and process incoming streams."""
    cfg = config.as_dict().get('twitter_api')
    auth = tweepy.OAuthHandler(cfg.get('consumer_key'), cfg.get('consumer_secret'))
    auth.set_access_token(cfg.get('access_token_key'), cfg.get('access_token_secret'))

    logger.debug('Init tweepy.Stream()')
    logger.debug(opts)
    listener = StreamListener(opts)
    streamer = tweepy.Stream(auth=auth, listener=listener, retry_count=9999,
        retry_time=1, buffer_size=16000)

    running = True
    while running:
        try:
            logger.info('Start streaming...')
            try:
                streamer.filter(track=opts.track, stall_warnings=True)
            except TypeError as e:
                if 'stall_warnings' in e.message:
                    logger.warn('Installed Tweepy version does not support stall_warnings parameter.  Restarting without stall warnings.')
                    streamer.filter(track=track)
                else:
                    raise

            logger.debug('Returned from streaming...')
        except IOError:
            logger.exception('Caught IOError')
        except KeyboardInterrupt:
            running = False
        except Exception:
            logger.exception("Unexpected exception.")

        if running:
            logger.debug('Sleeping...')
            time.sleep(5)


def _parse_command_line():
    parser = argparse.ArgumentParser(description='Twitter Stream dumper v%s' % _get_version())
    parser.add_argument(
        '-c',
        '--config-file',
        default='default.ini'
        )

    parser.add_argument(
        '-l',
        '--log-level',
        default='WARN',
        help="set log level to one used by logging module.  Default is WARN."
        )

#    parser.add_argument(
#        '-v',
#        '--verbosity',
#        action='count',
#        help='set verbosity level for various operations.  Default is non-verbose output.'
#        )

    parser.add_argument(
        '-r',
        '--report-lag',
        type=int,
        help='Report time difference between local system and Twitter stream server time exceeding this number of seconds.'
        )

    parser.add_argument(
        '-u',
        '--user-lang',
        type=csv_args,
        default='en',
        help="""BCP-47 language filter(s).  A comma-separate list of language codes.
        Default is "en", which will include
        only tweets made by users having English (en) as their profile language.
        If set, incoming status user\'s language must match one these languages."""
        )

    parser.add_argument(
        '-n',
        '--no-retweets',
        action='store_true',
        help='If set, don\'t include statuses identified as retweets.'
        )

    parser.add_argument(
        '-f',
        '--fields',
        type=csv_args,
        help='List of fields to output as CSV columns.  If not set, output raw status text, a large JSON structure.')

    parser.add_argument(
        'track',
        nargs='+',
        default='testing',
        help='Status keywords to be tracked.'
        )
    return parser.parse_args()


if __name__ == "__main__":
    import argparse
    import config
    opts = _parse_command_line()
    conf = config.DictConfigParser()
    conf.read(opts.config_file)
    _init_logger(conf, opts)
    process_tweets(conf, opts)
