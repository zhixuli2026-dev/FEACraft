import { Component, Fragment } from "react";
import type { ErrorInfo, ReactNode } from "react";
import { reportClientError } from "../utils/clientError";

interface Props { children: ReactNode }
interface State { error: Error | null; generation: number; attempts: number; recovering: boolean }

export default class ClientErrorBoundary extends Component<Props, State> {
  state: State = { error: null, generation: 0, attempts: 0, recovering: false };
  private recoveryWindowStarted = 0;
  private recoveryTimer: number | null = null;

  static getDerivedStateFromError(error: Error) { return { error }; }

  componentDidCatch(error: Error, _info: ErrorInfo) {
    reportClientError(error);
    if (this.recoveryTimer !== null) window.clearTimeout(this.recoveryTimer);
    const now = Date.now();
    let attempts = this.state.attempts;
    if (now - this.recoveryWindowStarted >= 60000) {
      this.recoveryWindowStarted = now;
      attempts = 0;
    }
    if (attempts >= 3) {
      this.setState({ recovering: false });
      return;
    }
    const attempt = attempts + 1;
    this.setState({ attempts: attempt, recovering: true });
    this.recoveryTimer = window.setTimeout(() => {
      this.recoveryTimer = null;
      this.setState((current) => ({
        error: null, generation: current.generation + 1, recovering: false,
      }));
    }, attempt * 200);
  }

  componentWillUnmount() {
    if (this.recoveryTimer !== null) window.clearTimeout(this.recoveryTimer);
  }

  render() {
    if (this.state.error) {
      return (
        <div role="alert" data-recovery-attempt={this.state.attempts}
             data-display-generation={this.state.generation}
             data-recovering={this.state.recovering}
             style={{ margin: 40, padding: 20, background: "#fff1f2", color: "#9f1239",
               border: "1px solid #fda4af", borderRadius: 8 }}>
          <h2>界面发生错误</h2>
          <p>{this.state.error.message}</p>
          <p>{this.state.recovering
            ? `正在只重建显示层（${this.state.attempts}/3），不会重新发送模型请求。`
            : "已生成脱敏错误报告。自动恢复未成功时，可以重新加载界面继续工作。"}</p>
          <button onClick={() => location.reload()}>重新加载</button>
        </div>
      );
    }
    return <div data-display-generation={this.state.generation} style={{ display: "contents" }}>
      <Fragment key={this.state.generation}>{this.props.children}</Fragment>
    </div>;
  }
}
