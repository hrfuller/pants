// Copyright 2017 Pants project contributors (see CONTRIBUTORS.md).
// Licensed under the Apache License, Version 2.0 (see LICENSE).

use std;
use std::convert::TryInto;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use tokio::runtime::Runtime;

use futures::Future;

use crate::core::{Failure, TypeId};
use crate::handles::maybe_drop_handles;
use crate::nodes::{NodeKey, WrappedNode};
use crate::scheduler::Session;
use crate::tasks::{Rule, Tasks};
use crate::types::Types;
use boxfuture::{BoxFuture, Boxable};
use core::clone::Clone;
use fs::{safe_create_dir_all_ioerror, PosixFS};
use graph::{EntryId, Graph, NodeContext};
use parking_lot::RwLock;
use process_execution::{self, BoundedCommandRunner, CommandRunner};
use rand::seq::SliceRandom;
use reqwest;
use rule_graph::RuleGraph;
use std::collections::btree_map::BTreeMap;
use store::Store;

///
/// The core context shared (via Arc) between the Scheduler and the Context objects of
/// all running Nodes.
///
/// Over time, most usage of `ResettablePool` (which wraps use of blocking APIs) should migrate
/// to the Tokio `Runtime`. The next candidate is likely to be migrating PosixFS to tokio-fs once
/// https://github.com/tokio-rs/tokio/issues/369 is resolved.
///
pub struct Core {
  pub graph: Graph<NodeKey>,
  pub tasks: Tasks,
  pub rule_graph: RuleGraph<Rule>,
  pub types: Types,
  runtime: Arc<RwLock<Runtime>>,
  store: Store,
  pub command_runner: BoundedCommandRunner,
  pub http_client: reqwest::r#async::Client,
  pub vfs: PosixFS,
  pub build_root: PathBuf,
}

impl Core {
  pub fn new(
    root_subject_types: Vec<TypeId>,
    tasks: Tasks,
    types: Types,
    build_root: PathBuf,
    ignore_patterns: &[String],
    work_dir: PathBuf,
    local_store_dir: PathBuf,
    remote_store_servers: Vec<String>,
    remote_execution_server: Option<String>,
    remote_execution_process_cache_namespace: Option<String>,
    remote_instance_name: Option<String>,
    remote_root_ca_certs_path: Option<PathBuf>,
    remote_oauth_bearer_token_path: Option<PathBuf>,
    remote_store_thread_count: usize,
    remote_store_chunk_bytes: usize,
    remote_store_chunk_upload_timeout: Duration,
    remote_store_rpc_retries: usize,
    remote_execution_extra_platform_properties: BTreeMap<String, String>,
    process_execution_parallelism: usize,
    process_execution_cleanup_local_dirs: bool,
  ) -> Core {
    // Randomize CAS address order to avoid thundering herds from common config.
    let mut remote_store_servers = remote_store_servers;
    remote_store_servers.shuffle(&mut rand::thread_rng());

    let runtime =
      Arc::new(RwLock::new(Runtime::new().unwrap_or_else(|e| {
        panic!("Could not initialize Runtime: {:?}", e)
      })));
    // We re-use these certs for both the execution and store service; they're generally tied together.
    let root_ca_certs = if let Some(path) = remote_root_ca_certs_path {
      Some(
        std::fs::read(&path)
          .unwrap_or_else(|err| panic!("Error reading root CA certs file {:?}: {}", path, err)),
      )
    } else {
      None
    };

    // We re-use this token for both the execution and store service; they're generally tied together.
    let oauth_bearer_token = if let Some(path) = remote_oauth_bearer_token_path {
      Some(
        std::fs::read_to_string(&path)
          .unwrap_or_else(|err| panic!("Error reading root CA certs file {:?}: {}", path, err)),
      )
    } else {
      None
    };

    let local_store_dir = local_store_dir.clone();
    let store = safe_create_dir_all_ioerror(&local_store_dir)
      .map_err(|e| format!("Error making directory {:?}: {:?}", local_store_dir, e))
      .and_then(|()| {
        if remote_store_servers.is_empty() {
          Store::local_only(local_store_dir)
        } else {
          Store::with_remote(
            local_store_dir,
            &remote_store_servers,
            remote_instance_name.clone(),
            &root_ca_certs,
            oauth_bearer_token.clone(),
            remote_store_thread_count,
            remote_store_chunk_bytes,
            remote_store_chunk_upload_timeout,
            // TODO: Take a parameter
            store::BackoffConfig::new(Duration::from_millis(10), 1.0, Duration::from_millis(10))
              .unwrap(),
            remote_store_rpc_retries,
          )
        }
      })
      .unwrap_or_else(|e| panic!("Could not initialize Store: {:?}", e));

    let underlying_command_runner: Box<dyn CommandRunner> = match &remote_execution_server {
      Some(ref address) => Box::new(process_execution::remote::CommandRunner::new(
        address,
        remote_execution_process_cache_namespace.clone(),
        remote_instance_name.clone(),
        root_ca_certs.clone(),
        oauth_bearer_token.clone(),
        remote_execution_extra_platform_properties.clone(),
        // Allow for some overhead for bookkeeping threads (if any).
        process_execution_parallelism + 2,
        store.clone(),
      )),
      None => Box::new(process_execution::local::CommandRunner::new(
        store.clone(),
        work_dir.clone(),
        process_execution_cleanup_local_dirs,
      )),
    };

    let command_runner =
      BoundedCommandRunner::new(underlying_command_runner, process_execution_parallelism);

    let http_client = reqwest::r#async::Client::new();
    let rule_graph = RuleGraph::new(tasks.as_map(), root_subject_types);

    Core {
      graph: Graph::new(),
      tasks: tasks,
      rule_graph: rule_graph,
      types: types,
      runtime: runtime,
      store,
      command_runner,
      http_client,
      // TODO: Errors in initialization should definitely be exposed as python
      // exceptions, rather than as panics.
      vfs: PosixFS::new(&build_root, &ignore_patterns).unwrap_or_else(|e| {
        panic!("Could not initialize VFS: {:?}", e);
      }),
      build_root: build_root,
    }
  }

  pub fn store(&self) -> Store {
    self.store.clone()
  }

  ///
  /// Start running a Future on a tokio Runtime.
  ///
  pub fn spawn<F: Future<Item = (), Error = ()> + Send + 'static>(&self, future: F) {
    // Make sure to copy our (thread-local) logging destination into the task.
    // When a daemon thread kicks off a future, it should log like a daemon thread (and similarly
    // for a user-facing thread).
    let logging_destination = logging::get_destination();
    self
      .runtime
      .read()
      .executor()
      .spawn(futures::future::ok(()).and_then(move |()| {
        logging::set_destination(logging_destination);
        future
      }))
  }

  ///
  /// Run a Future and return its resolved Result.
  ///
  /// This should never be called from in a Future context, or any context where anyone may want to
  /// spawn something on the runtime using Core::spawn.
  ///
  pub fn block_on<
    Item: Send + 'static,
    Error: Send + 'static,
    F: Future<Item = Item, Error = Error> + Send + 'static,
  >(
    &self,
    future: F,
  ) -> Result<Item, Error> {
    // Make sure to copy our (thread-local) logging destination into the task.
    // When a daemon thread kicks off a future, it should log like a daemon thread (and similarly
    // for a user-facing thread).
    let logging_destination = logging::get_destination();
    self
      .runtime
      .write()
      .block_on(futures::future::ok(()).and_then(move |()| {
        logging::set_destination(logging_destination);
        future
      }))
  }
}

#[derive(Clone)]
pub struct Context {
  pub entry_id: EntryId,
  pub core: Arc<Core>,
  pub session: Session,
}

impl Context {
  pub fn new(entry_id: EntryId, core: Arc<Core>, session: Session) -> Context {
    Context {
      entry_id: entry_id,
      core: core,
      session: session,
    }
  }

  ///
  /// Get the future value for the given Node implementation.
  ///
  pub fn get<N: WrappedNode>(&self, node: N) -> BoxFuture<N::Item, Failure> {
    // TODO: Odd place for this... could do it periodically in the background?
    maybe_drop_handles();
    self
      .core
      .graph
      .get(self.entry_id, self, node.into())
      .map(|node_result| {
        node_result
          .try_into()
          .unwrap_or_else(|_| panic!("A Node implementation was ambiguous."))
      })
      .to_boxed()
  }
}

impl NodeContext for Context {
  type Node = NodeKey;

  ///
  /// Clones this Context for a new EntryId. Because the Core of the context is an Arc, this
  /// is a shallow clone.
  ///
  fn clone_for(&self, entry_id: EntryId) -> Context {
    Context {
      entry_id: entry_id,
      core: self.core.clone(),
      session: self.session.clone(),
    }
  }

  fn graph(&self) -> &Graph<NodeKey> {
    &self.core.graph
  }

  fn spawn<F>(&self, future: F)
  where
    F: Future<Item = (), Error = ()> + Send + 'static,
  {
    self.core.runtime.read().executor().spawn(future);
  }
}
