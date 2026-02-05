from tornado import websocket, web, ioloop, httpserver
from tornado.log import gen_log
import asyncio
import concurrent.futures
from contextlib import asynccontextmanager
from pyee.asyncio import AsyncIOEventEmitter as EventEmitter
import json
import datetime as dt
import sys
from pprint import pprint, pformat
from collections import Counter, namedtuple, defaultdict
import re

ABNF_INLINE = "abnf-inline"
ESGF_INLINE = "esgf-inline"
XML_INLINE = "xml-inline"


def isidentifier(i):
    return re.match(r"^[^\d\W]\w*\Z", i) is not None


class SLUAttribute(object):
    pass


Prompt = namedtuple("Prompt", "text engine voice gain then_asr_recognize pause_before pause_after", defaults=(None,)*7)


class Entity(namedtuple('Entity', 'value entity_type conf begin end')):
    __slots__ = ()
    def time_sorting_key(self):
        # the longer Entity instances come first
        return (self.begin, -self.end)


class EntityMap(object):
    def __init__(self, entity_list):
        self._dict = defaultdict(list)

        self._entity_list = []
        for value in entity_list:
            key = value.value
            self._dict[key].append(value)
            self._entity_list.append(value)

        self._entity_list.sort(key=Entity.time_sorting_key)
        for key in self._dict:
            self._dict[key].sort(key=Entity.time_sorting_key)
        
    def __getitem__(self, key):
        return self._dict[key]

    def __repr__(self):
        return "{}({})".format(self.__class__.__name__, self._entity_list)

    def __len__(self):
        return len(self._dict)

    def has_key(self, k):
        return k in self._dict

    def keys(self):
        return self._dict.keys()

    def values(self):
        return (i for i in self._entity_list)

    def items(self):
        return ((i.value, i) for i in self._entity_list)

    def __cmp__(self, other):
        return self.__cmp__(self._dict, other)

    def __contains__(self, item):
        return item in self._dict

    def __iter__(self):
        return iter(self._dict)

    def __unicode__(self):
        return unicode(repr(self))
    
    @property
    def first(self):
        try:
            return self._entity_list[0].value
        except IndexError:
            return None
    
    @property
    def last(self):
        try:
            return self._entity_list[-1].value
        except IndexError:
            return None

    @property
    def all(self):
        return [i.value for i in self._entity_list]


class SLUResult(object):
    def __init__(self, obj, entities_with_mapping):
        if obj is not None:
            self._segments = obj["segments"]
        else:
            self._segments = []

        self._entities_with_mapping = entities_with_mapping

        self.first = SLUAttribute()
        self.last = SLUAttribute()
        self.all = SLUAttribute()

        if obj is not None:
            self.asr_result = obj["asr_result"]
        else:
            self.asr_result = None

        self._extract_results()

    def _extract_segment(self, segment):
        # Returns Counter of (mapped) tags in a given SED segment
        ret_by_type = {}

        for entity, conf in segment.items():
            entity_type, tags = entity.split(":", 1)
            if entity_type not in ret_by_type:
                ret_by_type[entity_type] = Counter()

            mapping = self._entities_with_mapping[entity_type]
            if mapping is None:
                value = tags
            else:
                value = mapping(tags)

            ret_by_type[entity_type][value] += conf
        return ret_by_type

    def _extract_results(self):
        first = self.first.__dict__
        last = self.last.__dict__
        all = self.all.__dict__

        new_segments = []

        for (begin, end), segment in self._segments:
            new_segment = []
            new_segments.append(new_segment)

            entity_hash = self._extract_segment(segment)
            for entity_type, entity_hash in entity_hash.items():
                (value, conf), = entity_hash.most_common(1)

                entity = Entity(value=value, entity_type=entity_type, conf=conf, begin=begin, end=end)
                new_segment.append(entity)

                if entity_type not in first:
                    # first value of entity_type
                    first[entity_type] = value
                last[entity_type] = value
                all.setdefault(entity_type, [])
                all[entity_type].append(value)

        entities = {}

        for entity_type in self._entities_with_mapping:
            first.setdefault(entity_type, None)
            last.setdefault(entity_type, None)
            all.setdefault(entity_type, [])
            entities[entity_type] = []

        entity_1best = []
        entity_types = []
    
        for segment in new_segments:
            if segment:
                segment.sort(key=lambda i:-i.conf)
                best = segment[0]
                entity_1best.append(best)
                entity_types.append(best.entity_type)

            for e in segment:
                entities[e.entity_type].append(e)

        self.entities = {}
        for key, value in entities.items():
            self.entities[key] = EntityMap(value)
            setattr(self, key, self.entities[key])

        self.entity_1best = EntityMap(entity_1best)
        self.entity_types = entity_types
    
    def __len__(self):
        return len(self.entity_1best)


class SpeechCloudWS(websocket.WebSocketHandler, EventEmitter):
    def check_origin(self, origin):
        return True

    def initialize(self, dialog_class):
        self.rtt_delay = 0.
        self.dialog_class = dialog_class

    def open(self):
        gen_log.info("Creating dialog manager as instance of %s", self.dialog_class)
        self._task = None
        self.dm = self.dialog_class(self)

    def on_close(self):
        gen_log.info("Canceling dialog manager task (WebSocket closed)")
        if self._task is not None:
            self._task.cancel()
        self.dm._finished()

    def _check_task_result(self, task):
        try:
            task.result()
        except (concurrent.futures.CancelledError, asyncio.CancelledError):
            gen_log.info("Dialog manager was canceled")
        except:
            self.log_dialog_exception(sys.exc_info())
        finally:
            self.close()

    def log_dialog_exception(self, exc_info):
        self.log_exception(*exc_info)
        
    async def on_message(self, msg):
        # Load the JSON
        msg = json.loads(msg)

        # Decide according to the message type
        msg_type = msg.pop('type')
        if msg_type == "sc_activate":
            await self._init_API_schema()
        elif msg_type == 'sc_start_session':
            # Initialize methods & events
            schema = self._prepare_dm_methods_events(msg.pop("schema"))
            self._init_API_methods(schema["methods"])
            self._init_API_events(schema["events"])
            self._task = asyncio.create_task(self.dm._main(msg))
            self._task.add_done_callback(self._check_task_result)
            self.emit(msg_type, **msg)
        else:
            # Emit the event
            self.emit(msg_type, **msg)

    def _create_method(self, method, schema):
        # Generic method creating the wrapper for SpeechCloud methods
        async def func(**kwargs):
            msg = kwargs
            msg['type'] = method
            await self.write_message(msg)

        func.__name__ = str(method)
        func.__doc__ = schema.get('description')
        return func

    def _create_event(self, event, schema):
        # Generic method creating the wrapper for SpeechCloud events
        def func():
            future = asyncio.Future()
            def handler(**kwargs):
                try:
                    return future.set_result(kwargs)
                except asyncio.InvalidStateError:
                    # Event was canceled
                    pass

            self.once(event, handler)
            return future

        func.__name__ = str(event)
        func.__doc__ = schema.get('description')
        return func

    def _prepare_dm_methods_events(self, schema):
        # Swap DM methods and events in the schema
        dm_events = self.dm_schema.get("events", set())
        dm_methods = self.dm_schema.get("methods", set())

        new_schema = schema.copy()
        new_schema["methods"] = new_methods = {}
        new_schema["events"] = new_events = {}

        for method, method_schema in schema["methods"].items():
            if method not in dm_methods:
                new_methods[method] = method_schema
            else:
                new_events[method] = method_schema

        for event, event_schema in schema["events"].items():
            if event not in dm_events:
                new_events[event] = event_schema
            else:
                new_methods[event] = event_schema

        return new_schema

    def _init_API_methods(self, methods):
        self._api_methods = methods

        for method, schema in methods.items():
            func = self._create_method(method, schema)
            setattr(self, method, func)

    def _init_API_events(self, events):
        self._api_events = events

        for event, schema in events.items():
            func = self._create_event(event, schema)
            setattr(self, event, func)

    async def _init_API_schema(self):
        self.dm_schema = self.dm.get_schema()
        await self.write_message({"type": "sc_activated", "schema": self.dm_schema})

    def available_methods(self):
        return self._api_methods

    def available_event(self):
        return self._api_events

    @classmethod
    def run(cls, dialog_class, address, port, path="/ws", static_path=None, static_route="/static/(.*)", ssl_options=None):
        app_config = [
            (path, cls, {"dialog_class": dialog_class}),
            ]

        if static_path is not None:
            app_config.append( (static_route, NoCacheStaticFileHandler, {"path": static_path}) )
        app = web.Application(app_config)

        http_server = httpserver.HTTPServer(app,
            ssl_options=ssl_options
        )

        http_server.listen(port, address=address)

        ioloop.IOLoop.instance().start()

class NoCacheStaticFileHandler(web.StaticFileHandler):
    def set_extra_headers(self, path):
        self.set_header("Cache-control", "no-cache")


class Dialog(object):
    FIRST_PROMPT_CACHEABLE = True

    def __init__(self, sc):
        super(Dialog, self).__init__()
        self.sc = sc
        self._slu_entities = None
        self.logger = gen_log

        # Set meaningful details
        self.schema_uri = None
        self.session_id = None
        self.session_uri = None

    def get_schema(self):
        return {'description': 'Dialog manager schema',
                  'events': {'dm_display': {'additionalProperties': False,
                                            'description': 'Event for displaying arbitrary string in the client application',
                                            'properties': {'type': {'enum': ['dm_display']},
                                                           'text': {
                                                                'type': 'string'
                                                            },
                                                          },
                                            'required': ['type', 'text'],
                                            'title': 'dm_display',
                                            'type': 'object'},
                              'dm_receive_message': {'additionalProperties': False,
                                            'description': 'Event for receiving arbitrary data from dialog manager',
                                            'properties': {'type': {'enum': ['dm_receive_message']},
                                                           'data': {},
                                                          },
                                            'required': ['type', 'data'],
                                            'title': 'dm_receive_message',
                                            'type': 'object'}
                            },
                  'methods': {'dm_send_message': {'additionalProperties': False,
                                            'description': 'Method for sending arbitrary data to dialog manager',
                                            'properties': {'type': {'enum': ['dm_send_message']},
                                                           'data': {},
                                                          },
                                            'required': ['type', 'data'],
                                            'title': 'dm_send_message',
                                            'type': 'object'}
                            },
                   "oneOf": [{"$ref": "#/events/dm_display"},
                             {"$ref": "#/events/dm_receive_message"},
                             {"$ref": "#/methods/dm_send_message"}
                            ], 
        }

    
    def _convert_prompts(self, text, **kwargs):
        if not isinstance(text, list):
            text = [text]

        for prompt in text:
            if not isinstance(prompt, Prompt):
                prompt = Prompt(text=prompt)
            yield prompt
        

    async def synthesize(self, text, _wait_for_done=False, **kwargs):
        for prompt in self._convert_prompts(text, **kwargs):
            prompt_kwargs = dict(**kwargs)
            pause_before = 0
            pause_after = 0
            for key, value in prompt._asdict().items():
                if key == "pause_before":
                    pause_before = value
                elif key == "pause_after":
                    pause_after = value
                elif value is not None:
                    prompt_kwargs[key] = value
            if pause_before:
                await asyncio.sleep(pause_before)
            await self.sc.tts_synthesize(**prompt_kwargs)
            if _wait_for_done:
                await self.sc.tts_done()
            if pause_after:
                await asyncio.sleep(pause_after)
        
    async def synthesize_and_wait(self, text, **kwargs):
        return await self.synthesize(text, _wait_for_done=True, **kwargs)

    async def _wait_for_speech(self, min_speech_frames):
        remaining = min_speech_frames
        while True:
            result = await self.sc.asr_signal()
            if result["speech"]:
                remaining -= 1
            if remaining <= 0:
                return True

    async def _wait_for_asr_result(self):
        while True:
            result = await self.sc.asr_result()
            if result["partial_result"]:
                continue
            words = result["word_1best"]
            return not bool(words), result

    async def _wait_for_slu_entities(self):
        while True:
            result = await self.sc.slu_entities()
            if result["partial_result"]:
                continue
            classes = result["classes"]
            return not bool(classes), result

    async def _wait_for_result(self, result_method, timeout=None):
        # if we observe at least min_speech_frames
        # we prolong the timeout to max_speech_timeout 
        # because the user may speak longer than timeout
        min_speech_frames = 5
        max_speech_timeout = timeout * 5 if timeout is not None else None

        if timeout is not None:
            deadline = dt.datetime.now() + dt.timedelta(seconds=timeout)
        else:
            deadline = None

        speech_task = None
        result_task = None

        try:
            while True:
                if deadline:
                    timeout = (deadline-dt.datetime.now()).total_seconds()
                    if timeout <= 0:
                        # the overall timeout is over
                        return None
                else:
                    timeout = None

                # instantiate Task
                if speech_task is None or speech_task.done():
                    speech_task = asyncio.create_task(self._wait_for_speech(min_speech_frames=min_speech_frames))
                if result_task is None or result_task.done():
                    result_task = asyncio.create_task(result_method())

                done, pending = await asyncio.wait({speech_task, result_task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                if speech_task in done:
                    # wait for asr_result with max_speech_timeout 
                    done, pending = await asyncio.wait({result_task}, timeout=max_speech_timeout)

                if result_task in done:
                    is_empty, result = result_task.result()
                    if not is_empty:
                        # we have non-empty result
                        return result
                    else:
                        # wait for next result, maybe it will be non-empty, otherwise the timeout will be shortened
                        continue
                else:
                    # max_speech_timeout is over
                    return None
        finally:
            speech_task.cancel()
            result_task.cancel()

    async def wait_for_asr_result(self, timeout=None):
        return await self._wait_for_result(self._wait_for_asr_result, timeout)

    async def wait_for_slu_result(self, timeout=None):
        # TODO: better checking
        assert self._slu_entities is not None

        slu_result = await self._wait_for_result(self._wait_for_slu_entities, timeout)
        slu_result = SLUResult(slu_result, self._slu_entities)
        return slu_result

    async def recognize_and_wait_for_asr_result(self, timeout=None):
        await self.sc.asr_recognize()
        try:
            return await self.wait_for_asr_result(timeout)
        finally:
            await self.sc.asr_pause()

    async def recognize_and_wait_for_slu_result(self, timeout=None):
        await self.sc.asr_recognize()
        try:
            return await self.wait_for_slu_result(timeout)
        finally:
            await self.sc.asr_pause()

    async def _synthesize_and_wait_for_result(self, awaitable_method, text, timeout=None, **kwargs):
        await self.synthesize_and_wait(text, **kwargs)

        self.logger.debug("Sleeping for RTT delay: %.3f [s]", self.sc.rtt_delay)
        await asyncio.sleep(self.sc.rtt_delay)
        await self.sc.asr_recognize()

        await self.sc.asr_recognizing()

        try:
            return await awaitable_method(timeout)
        finally:
            await self.sc.asr_pause()

    async def synthesize_and_wait_for_asr_result(self, text, timeout=None, **kwargs):
        return await self._synthesize_and_wait_for_result(self.wait_for_asr_result, text, timeout, **kwargs)

    async def synthesize_and_wait_for_slu_result(self, text, timeout=None, **kwargs):
        return await self._synthesize_and_wait_for_result(self.wait_for_slu_result, text, timeout, **kwargs)

    async def define_slu_grammars(self, grammars):
        task_done = self.sc.slu_set_grammars_done()
        task_error = self.sc.sc_error()

        slu_entities = {grm["entity"]: grm.pop("mapping", None) for grm in grammars}

        await self.sc.slu_set_grammars(grammars=grammars)

        done, pending = await asyncio.wait({task_done, task_error}, return_when=asyncio.FIRST_COMPLETED)
        if task_done in done:
            # Everything is Ok
            task_error.cancel()
            self._slu_entities = slu_entities
            return
        else:
            # Error in SpeechCloud
            task_done.cancel()
            result = task_error.result()
            raise ValueError(result["error"])

    async def use_slu_grammars(self, entity_dict_or_list):
        if not isinstance(entity_dict_or_list, dict):
            entity_dict_or_list = {i: None for i in entity_dict_or_list}
        slu_entities = {entity: mapping for (entity, mapping) in entity_dict_or_list.items()}
        self._slu_entities = slu_entities

    def grammar_from_dict(self, entity, grm_dict, weight=1.0):
        if not isidentifier(entity):
            raise ValueError(f"Not an identifier: {entity}")

        inverse_map = {}

        root_terms = []
        for idx, (target, terms) in enumerate(grm_dict.items(), 1):
            if isinstance(terms, str):
                terms = [terms]

            abnf_tag = f"{entity}_{idx:03d}"
            inverse_map[abnf_tag] = target

            grm_terms = []
            for term in terms:
                grm_terms.append(term)
            grm_terms = " | ".join(grm_terms)

            root_terms.append(f"( ({grm_terms}) {{{abnf_tag}}} )")

        root_terms = " | ".join(root_terms)
        grm = f"#ABNF 1.0 utf-8;\n" \
              f"root ${entity};\n" \
              f"public ${entity} = {root_terms};\n" \

        return [{"type": ABNF_INLINE,
                "entity": entity,
                "data": grm,
                "mapping": inverse_map.get,
                "weight": weight}]

    async def display(self, text):
        self.logger.debug(f"{text}")
        text_debug = f'[DEBUG]:\t{text}'
        return await self.sc.dm_display(text=text_debug)

    async def send_message(self, data):
        # Emits dm_receive_message event in all connected clients
        return await self.sc.dm_receive_message(data=data)
    
    def on_receive_message(self, data):
        self.logger.debug("Received message:\n{}".format(pformat(data)))

    async def pop_message(self, timeout=None):
        msg_task = self.sc.dm_send_message()
        done, pending = await asyncio.wait({msg_task}, timeout=timeout)
        if msg_task not in done:
            # No message popped from the client
            msg_task.cancel()
            return None
        else:
            return msg_task.result()

    async def start_session(self):
        pass

    async def _main(self, start_session_message):
        self.schema_uri = start_session_message["schema_uri"]
        self.session_id = start_session_message["session_id"]
        self.session_uri = start_session_message["session_uri"]

        self.logger.debug("Started DM session %s, session log: %s?format=yaml.html", self.session_id, self.session_uri)
        self.logger.debug("SpeechCloud application schema: %s?format=docson", self.schema_uri)

        await self.start_session()

        # Connect some sc.* events with the Dialog methods
        # the logic is inverted, what is method for other clients is event for dialog manager
        self.sc.on("dm_send_message", self.on_receive_message)

        await self.sc.asr_ready()
        return await self.main()

    async def main(self):
        pass

    async def end_session(self):
        pass

    def websocket_closed(self):
        self.logger.debug("DM session %s websocket closed", self.session_id)

    def _finished(self):
        if self.session_id is not None:
            self._task = asyncio.create_task(self.end_session())
            self._task.add_done_callback(self._check_finished_result)
        self.websocket_closed()

    def _check_finished_result(self, task):
        try:
            task.result()
        except:
            self.logger.exception("Exception in end_session() was ignored:")
        finally:
            self.logger.debug("DM session %s finished", self.session_id)
