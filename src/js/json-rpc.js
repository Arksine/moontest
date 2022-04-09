// JSON-RPC Client implementation
//
// NOTE:  JSON-RPC isn't designed for bi-directional calls, however Moonraker
// uses JSON-RPC to send events to clients.  As such, this implementation also
// includes a "mostly complete" JSON-RPC server.  The JSON-RPC spec defines errors
// that should be returned for invalid rpc objects.  This implementation does not
// send such errors back to Moonraker as it not possible to determine if the
// error was the result of an invalid request or an invalid response. Generally
// this doesn't matter as "notifications" do not expect a response.
//
// The exception to this is for "agent" client types.  Agents may expose their
// own JSON-RPC methods that Moonraker will call and expect a response for.
// Should the agent receive an invalid object (JSON parsing error, missing
// required field, etc) it is recommended that the agent log the error so further
// debugging can be done.  The agent MUST however return errors such as method not
// found, invalid parameters, or an error executing the requested method.

export default class JsonRPC {
    constructor() {
       this.id_counter = 0;
       this.methods = new Object();
       this.pending_callbacks = new Object();
       this.transport = null;
    }

    _create_uid() {
        let uid = this.id_counter;
        this.id_counter++;
        return uid.toString();
    }

    _build_request(method_name, uid, kwargs, ...args) {
        let request = {
            jsonrpc: "2.0",
            method: method_name};
        if (uid != null) {
            request.id = uid;
        }
        if (kwargs != null) {
            request.params = kwargs
        }
        else if (args.length > 0) {
            request.params = args;
        }
        return request;
    }

    _build_error(message, code, request) {
        let error = {
            jsonrpc: "2.0",
            error: {
                message: message,
                code: code
            },
            id: null
        };
        if ("id" in request) {
            error.id = request.id;
        }
        console.log(`JSON-RPC Request Error: ${code} ${message}`);
        console.log(request);
        return error;
    }

    register_method(method_name, method) {
        this.methods[method_name] = method
    }

    register_transport(transport) {
        // The transport must have a send method.  It should
        // have an onmessage callback that fires when it
        // receives data, but it would also be valid to directly call
        // JsonRPC.process_received if necessary
        this.transport = transport;
        this.transport.onmessage = this.process_received.bind(this)
    }

    send_batch_request(requests) {
        // Batch requests take an array of requests.  Each request
        // should be an object with the following attribtues:
        // 'method' - The name of the method to execture
        // 'type' - May be "request" or "notification"
        // 'params' - method parameters, if applicable
        //
        // If a method has no parameters then the 'params' attribute
        // should not be included.

        if (this.transport == null)
            return Promise.reject(Error("No Transport Initialized"));

        let batch_request = [];
        let promises = [];
        requests.forEach((request, idx) => {
            let name = request.method;
            let args = [];
            let kwargs = null;
            let uid = null;
            if ('params' in request) {
                if (request.params instanceof Object)
                    kwargs = request.params;
                else
                    args = request.params;
            }
            if (request.type == "request") {
                uid = this._create_uid();
                promises.push(new Promise((resolve, reject) => {
                    this.pending_callbacks[uid] = (result, error) => {
                        let response = {method: name, index: idx};
                        if (error != null) {
                            response.error = error;
                            reject(response);
                        } else {
                            response.result = result;
                            resolve(response);
                        }
                    }
                }));
            }
            batch_request.push(this._build_request(
                name, uid, kwargs, ...args));
        });

        this.transport.send(JSON.stringify(batch_request));
        return Promise.all(promises);
    }

    call_method(method_name, ...args) {
        let uid = this._create_uid();
        let request = this._build_request(
            method_name, uid, null, ...args);
        if (this.transport != null) {
            this.transport.send(JSON.stringify(request));
            return new Promise((resolve, reject) => {
                this.pending_callbacks[uid] = (result, error) => {
                    if (error != null) {
                        reject(error);
                    } else {
                        resolve(result);
                    }
                }
            });
        }
        return Promise.reject(Error("No Transport Initialized"));
    }

    call_method_with_kwargs(method_name, kwargs) {
        let uid = this._create_uid();
        let request = this._build_request(method_name, uid, kwargs);
        if (this.transport != null) {
            this.transport.send(JSON.stringify(request));
            return new Promise((resolve, reject) => {
                this.pending_callbacks[uid] = (result, error) => {
                    if (error != null) {
                        reject(error);
                    } else {
                        resolve(result);
                    }
                }
            });
        }
        return Promise.reject(Error("No Transport Initialized"));
    }

    notify(method_name, ...args) {
        let notification = this._build_request(
            method_name, null, null, ...args);
        if (this.transport != null) {
            this.transport.send(JSON.stringify(notification));
        }
    }

    process_received(encoded_data) {
        let rpc_data = JSON.parse(encoded_data);
        if (rpc_data instanceof Array) {
            // batch request/response
            for (let data of rpc_data) {
                this._validate_and_dispatch(data);
            }
        } else {
            this._validate_and_dispatch(rpc_data);
        }
    }

    _validate_and_dispatch(rpc_data) {
        if (rpc_data.jsonrpc != "2.0") {
            console.log("Invalid JSON-RPC data");
            console.log(rpc_data);
            return;
        }

        if ("result" in rpc_data || "error" in rpc_data) {
            // This is a response to a client request
            this._handle_response(rpc_data);
        } else if ("method" in rpc_data) {
            // This is a server side notification/event
            this._handle_request(rpc_data);
        } else {
            // Invalid RPC data
            console.log("Invalid JSON-RPC data");
            console.log(rpc_data);
        }
    }

    _handle_request(request) {
        let method = this.methods[request.method];
        let response = null;
        let ret = null;
        if (method == null) {
            console.log("Invalid Method: " + request.method);
            return;
        }
        try {
            if ("params" in request) {
                let args = request.params;
                if (args instanceof Array)
                    ret = method(...args);
                else if (args instanceof Object) {
                    ret = method(args);
                } else {
                    response = this._build_error(
                        "Invalid Parameters", -32602, request
                    );
                }
            } else {
                ret = method();
            }
            if ("id" in request && response == null) {
                // TODO: send response
                response =  {jsonrpc: "2.0", result: ret, id: request.id};
            }
        } catch (error) {
            let msg = "Server Error"
            if ("message" in error && error.message != "")
                msg = error.message;
            response = this._build_error(msg, -31000, request);
        }
        if (response != null && this.transport != null) {
            this.transport.send(JSON.stringify(response));
        }
    }

    _handle_response(response) {
        if (response.result != null && response.id != null) {
            let uid = response.id;
            let response_finalize = this.pending_callbacks[uid];
            if (response_finalize != null) {
                response_finalize(response.result);
                delete this.pending_callbacks[uid];
            } else {
                console.log("No Registered RPC Call for uid:");
                console.log(response);
            }
        } else if (response.error != null) {
            // Check ID, depending on the error it may or may not be available
            let uid = response.id;
            let response_finalize = this.pending_callbacks[uid];
            if (response_finalize != null) {
                response_finalize(null, response.error);
                delete this.pending_callbacks[uid];
            } else {
                console.log("JSON-RPC error recieved");
                console.log(response.error);
            }
        } else {
            console.log("Invalid JSON-RPC response");
            console.log(response);
        }
    }
}