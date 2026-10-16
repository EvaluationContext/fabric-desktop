fn main() {
    // CI sets FABRIC_RELEASE_COMMIT for builds of main. The app compares it with
    // the latest release to offer updates; local builds leave it unset and skip that.
    println!("cargo:rerun-if-env-changed=FABRIC_RELEASE_COMMIT");
    let commit = std::env::var("FABRIC_RELEASE_COMMIT").unwrap_or_default();
    println!("cargo:rustc-env=FABRIC_BUILD_COMMIT={commit}");
    // The Microsoft Entra app the app signs in with unless the organization or the person
    // sets another (repository variable FABRIC_CLIENT_ID; empty means people paste one in Settings).
    println!("cargo:rerun-if-env-changed=FABRIC_CLIENT_ID");
    let client_id = std::env::var("FABRIC_CLIENT_ID").unwrap_or_default();
    println!("cargo:rustc-env=FABRIC_BUILTIN_CLIENT_ID={client_id}");
    tauri_build::build()
}
