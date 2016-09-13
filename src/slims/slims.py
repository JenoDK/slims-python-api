import base64
import os
import logging
import requests
import sched
import time
import threading
from flask import request as flaskrequest
from flask import Flask, jsonify
from werkzeug.local import Local

from .flowrun import FlowRun

app = Flask(__name__)
slims_instances = {}
local = Local()
logger = logging.getLogger('genohm.slims.slims')
logging.basicConfig(level=logging.INFO)


def slims_local():
    return local


@app.route("/<name>/<operation>/<step>", methods=["POST"])
def start_step(name, operation, step):
    data = flaskrequest.json
    flow_information = data['flowInformation']

    logger.info("Executing " + str(flow_information['flowId']) + " step " + step)
    return_value = slims_instances[name]._execute_operation(operation, step, data)
    if return_value:
        return jsonify(**return_value)
    else:
        return jsonify(**{})


def flask_thread(port):
    app.run(port=port)


class SlimsApiException(Exception):
    pass


class SlimsApi(object):

    def __init__(self, url, username, password, repo_location):
        self.url = url + "/rest/"
        self.username = username
        self.password = password
        self.repo_location = repo_location

    def get_entities(self, url, body=None):
        if not url.startswith(self.url):
            url = self.url + url

        response = requests.get(url,
                                auth=(self.username, self.password),
                                headers=SlimsApi._headers(),
                                json=body)
        records = []
        if response.status_code == 200:
            for entity in response.json()["entities"]:
                if entity["tableName"] == "Attachment":
                    records.append(Attachment(entity, self))
                else:
                    records.append(Record(entity, self))
            return records
        else:
            raise SlimsApiException("Could not fetch entities: " + response.text)

    def get(self, url):
        return requests.get(self.url + url,
                            auth=(self.username, self.password), headers=SlimsApi._headers())

    def post(self, url, body):
        return requests.post(self.url + url, json=body,
                             auth=(self.username, self.password), headers=SlimsApi._headers())

    def put(self, url, body):
        return requests.put(self.url + url, json=body,
                            auth=(self.username, self.password), headers=SlimsApi._headers())

    def delete(self, url):
        return requests.delete(self.url + url,
                               auth=(self.username, self.password), headers=SlimsApi._headers())

    @staticmethod
    def _headers():
        try:
            return {'X-SLIMS-REQUESTED-FOR': local.user}
        except AttributeError:
            return {}


class Slims(object):

    def __init__(self,
                 name,
                 url,
                 username=None,
                 password=None,
                 token=None,
                 repo_location=None,
                 local_host="localhost",
                 local_port=5000):
        slims_instances[name] = self
        if username is not None and password is not None:
            self.slims_api = SlimsApi(url, username, password, repo_location)
        elif token is not None:
            self.slims_api = SlimsApi(url, "TOKEN", token, repo_location)
        else:
            raise Exception("Either specify a username and a password or a token")

        self.name = name
        self.operations = {}
        self.flow_definitions = []
        self.refresh_flows_thread = threading.Thread(target=self._refresh_flows_thread_inner)
        self.refresh_flows_thread.daemon = True
        self.local_host = local_host
        self.local_port = local_port

    def fetch(self, table, criteria, sort=[], start=None, end=None):
        """Allows to fetch data that match criterion

        Parameters:
        table -- name of the table in which the fetch takes place
        criteria -- criteria to fetch
                    it calls criteria functions
                    criterion can be added using one junction function followed
                    by add(criteria) function
        sort -- list of the fields used to sort
        start --  number representing the position in a list of the first result to display
        end  --  number representing the position in a list of the last result to display
        """
        body = {
            "sortBy": sort,
            "startRow": start,
            "endRow": end,
        }
        if criteria:
            body["criteria"] = criteria.to_dict()

        return self.slims_api.get_entities(table + "/advanced", body=body)

    def fetch_by_pk(self, table, pk):
        entities = self.slims_api.get_entities(table + "/" + str(pk))
        if len(entities) > 0:
            return entities[0]
        else:
            return None

    def add(self, table, values):
        response = self.slims_api.put(url=table, body=values)
        if response.status_code == 200:
            new_values = response.json()["entities"][0]
            return Record(new_values, self.slims_api)
        else:
            raise Exception(response.text)

    def add_flow(self, flow_id, name, usage, steps, testing=False):
        """Allows to add a SLimsGate flow in SLims interface.

        Parameters:
        flow_id -- name of the id of the flow_id
        name -- name of the flow that will be displayed in SLims interface
        usage -- name indicating in which table the flow can be called
        steps -- a list of steps elements that needs to be executed
        """
        step_dicts = []
        i = 0
        for step in steps:
            url = flow_id + "/" + repr(i)
            step_dicts.append(step.to_dict(url))
            self.operations[url] = step
            i += 1

        flow = {'id': flow_id, 'name': name, 'usage': usage, 'steps': step_dicts, 'pythonApiFlow': True}
        self.flow_definitions.append(flow)
        self._register_flows([flow], False)

        if not testing:
            if not self.refresh_flows_thread.is_alive():
                self.refresh_flows_thread.start()
            flask_thread(self.local_port)

    def _register_flows(self, flows, is_reregister):
        flow_ids = map(lambda flow: flow.get('id'), flows)
        verb = "re-register" if is_reregister else "register"

        try:
            instance = {'url': "http://" + self.local_host + ':' + str(self.local_port), 'name': self.name}
            body = {'instance': instance, 'flows': flows}
            response = self.slims_api.post("external/", body)

            if response.status_code == 200:
                logger.info("Successfully " + verb + "ed " + str(flow_ids))
            else:
                logger.info("Could not " + verb + " " + str(flow_ids) +
                            " (HTTP Response code: " + str(response.status_code) + ")")
                try:
                    logger.info("Reason: " + response.json()["errorMessage"])
                except Exception:
                    # Probably no json was sent
                    pass
        except Exception:
            logger.info("Could not " + verb + " flows " + str(flow_ids) + " trying again in 60 seconds")

    def _execute_operation(self, operation, step, data):
        flow_run = FlowRun(self.slims_api, step, data)
        output = self.operations[operation + "/" + str(step)].execute(flow_run)
        return output

    def _refresh_flows_thread_inner(self):
        def refresh_flows(scheduler):
            self._register_flows(self.flow_definitions, True)
            scheduler.enter(60, 1, refresh_flows, (scheduler,))

        scheduler = sched.scheduler(time.time, time.sleep)
        scheduler.enter(60, 1, refresh_flows, (scheduler,))
        scheduler.run()


class Record(object):

    def __init__(self, json_entity, slims_api):
        self.json_entity = json_entity
        self.slims_api = slims_api

        for json_column in json_entity["columns"]:
            column = Column(json_column)
            self.__dict__[column.name] = column

    def update(self, values):
        url = self.table_name() + "/" + str(self.pk())
        response = self.slims_api.post(url=url, body=values).json()
        new_values = response["entities"][0]
        return Record(new_values, self.slims_api)

    def remove(self):
        url = self.table_name() + "/" + str(self.pk())
        response = self.slims_api.delete(url=url)
        if response.status_code != 200:
            raise Exception("Delete failed: " + response.text)

    def table_name(self):
        return self.json_entity["tableName"]

    def pk(self):
        return self.json_entity["pk"]

    def attachments(self):
        return self.slims_api.get_entities(
            "attachment/" + self.json_entity["tableName"] + "/" + str(self.json_entity["pk"]))

    def add_attachment(self, name, byte_array):
        """Adds an attachment to a record (over HTTP)

        Example uses:
          * content.add_attachment("test.txt", b"Hi from python")
          * with open(file_name, 'rb') as to_upload:
                content.add_attachment("test.txt", to_upload.read())

        Parameters:
        name -- The name of the attachment
        byte_array -- The binary content of the attachment
        Returns:
        The primary key of the added attachment
        """

        body = {
            "attm_name": name,
            "atln_recordPk": self.pk(),
            "atln_recordTable": self.table_name(),
            "contents": base64.b64encode(byte_array).decode("utf-8")
        }
        response = self.slims_api.post(url="repo", body=body)
        location = response.headers['Location']
        return int(location[location.rfind("/") + 1:])

    def column(self, column_name):
        return self.__dict__[column_name]

    def follow(self, link_name):
        for link in self.json_entity["links"]:
            if link["rel"] == link_name:
                href = link["href"]
                entities = self.slims_api.get_entities(href)
                if link_name.startswith("-"):
                    return entities
                else:
                    if len(entities) > 0:
                        return entities[0]
                    else:
                        return None
        raise KeyError(str(link_name) + "not found in the list of links")


class Attachment(Record):

    def __init__(self, json_entity, slims_api):
        super(Attachment, self).__init__(json_entity, slims_api)

    def get_local_path(self):
        if self.slims_api.repo_location:
            return os.path.join(self.slims_api.repo_location, self.attm_path.value)
        else:
            raise RuntimeError("no repo_location configured")

    def download_to(self, location):
        """Downloads an attachment to a location on disk

        Example uses:
          * attachment.download_to("test.txt")

        Parameters:
        location -- The name of the file the attachment should be downloaded to
        """
        with open(location, 'wb') as destination:
            response = self.slims_api.get("repo/" + str(self.pk()))
            destination.write(response.content)


class Column(object):

    def __init__(self, json_column):
        self.__dict__.update(json_column)
